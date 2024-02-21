import logging
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Deque, Optional, TypedDict, TypeVar, cast

import sentry_sdk
from django.http import Http404, HttpRequest, HttpResponse
from rest_framework.exceptions import ParseError
from rest_framework.response import Response
from sentry_relay.consts import SPAN_STATUS_CODE_TO_NAME
from snuba_sdk import Column, Function

from sentry import constants, eventstore, features
from sentry.api.api_publish_status import ApiPublishStatus
from sentry.api.base import region_silo_endpoint
from sentry.api.bases import NoProjects, OrganizationEventsV2EndpointBase
from sentry.api.serializers.models.event import get_tags_with_meta
from sentry.api.utils import handle_query_errors
from sentry.eventstore.models import Event
from sentry.issues.issue_occurrence import IssueOccurrence
from sentry.models.group import Group
from sentry.models.organization import Organization
from sentry.search.events.builder import QueryBuilder, SpansIndexedQueryBuilder
from sentry.search.events.types import ParamsType, QueryBuilderConfig
from sentry.snuba import discover
from sentry.snuba.dataset import Dataset
from sentry.snuba.referrer import Referrer
from sentry.utils.dates import to_timestamp_from_iso_format
from sentry.utils.numbers import base32_encode, format_grouped_length
from sentry.utils.sdk import set_measurement
from sentry.utils.snuba import bulk_snql_query
from sentry.utils.validators import INVALID_ID_DETAILS, is_event_id

logger: logging.Logger = logging.getLogger(__name__)
MAX_TRACE_SIZE: int = 100


_T = TypeVar("_T")
NodeSpans = list[dict[str, Any]]
SnubaTransaction = TypedDict(
    "SnubaTransaction",
    {
        "id": str,
        "transaction.status": int,
        "transaction.op": str,
        "transaction.duration": int,
        "transaction": str,
        "timestamp": str,
        "trace.span": str,
        "trace.parent_span": str,
        "trace.parent_transaction": Optional[str],
        "root": str,
        "project.id": int,
        "project": str,
        "issue.ids": list[int],
    },
)
SnubaError = TypedDict(
    "SnubaError",
    {
        "id": str,
        "timestamp": str,
        "trace.span": str,
        "transaction": str,
        "issue.id": int,
        "title": str,
        "tags[level]": str,
        "project.id": int,
        "project": str,
    },
)


class TraceError(TypedDict):
    event_id: str
    issue_id: int
    span: str
    project_id: int
    project_slug: str
    title: str
    level: str
    timestamp: str
    event_type: str
    generation: int


class TracePerformanceIssue(TypedDict):
    event_id: str
    issue_id: int
    issue_short_id: str | None
    span: list[str]
    suspect_spans: list[str]
    project_id: int
    project_slug: str
    title: str
    level: str
    culprit: str
    type: int
    start: float | None
    end: float | None


LightResponse = TypedDict(
    "LightResponse",
    {
        "event_id": str,
        "span_id": str,
        "transaction": str,
        "transaction.duration": int,
        "transaction.op": str,
        "project_id": int,
        "project_slug": str,
        "parent_span_id": Optional[str],
        "parent_event_id": Optional[str],
        "generation": Optional[int],
        "errors": list[TraceError],
        "performance_issues": list[TracePerformanceIssue],
    },
)
FullResponse = TypedDict(
    "FullResponse",
    {
        "event_id": str,
        "span_id": str,
        "transaction": str,
        "transaction.duration": int,
        "transaction.op": str,
        "project_id": int,
        "project_slug": str,
        "parent_span_id": Optional[str],
        "parent_event_id": Optional[str],
        "profile_id": Optional[str],
        "generation": Optional[int],
        "errors": list[TraceError],
        "performance_issues": list[TracePerformanceIssue],
        "timestamp": str,
        "start_timestamp": str,
        # Any because children are more FullResponse objects
        "children": list[Any],
        # Only on the detailed response
        "measurements": dict[str, int],
        "tags": list[tuple[str, str]],
        "_meta": dict[str, Any],
        "transaction.status": str,
    },
)


class TraceEvent:
    def __init__(
        self,
        event: SnubaTransaction,
        parent: str | None,
        generation: int | None,
        light: bool = False,
        snuba_params: ParamsType | None = None,
        span_serialized: bool = False,
    ) -> None:
        self.event: SnubaTransaction = event
        self.errors: list[TraceError] = []
        self.children: list[TraceEvent] = []
        self.performance_issues: list[TracePerformanceIssue] = []

        # Can be None on the light trace when we don't know the parent
        self.parent_event_id: str | None = parent
        self.generation: int | None = generation

        # Added as required because getting the nodestore_event is expensive
        self._nodestore_event: Event | None = None
        self.fetched_nodestore: bool = span_serialized
        self.span_serialized = span_serialized
        if span_serialized:
            self.fetched_nodestore = True
        self.load_performance_issues(light, snuba_params)

    @property
    def nodestore_event(self) -> Event | None:
        with sentry_sdk.start_span(op="nodestore", description="get_event_by_id"):
            if self._nodestore_event is None and not self.fetched_nodestore:
                self.fetched_nodestore = True
                self._nodestore_event = eventstore.backend.get_event_by_id(
                    self.event["project.id"], self.event["id"]
                )
        return self._nodestore_event

    def load_performance_issues(self, light: bool, snuba_params: ParamsType) -> None:
        """Doesn't get suspect spans, since we don't need that for the light view"""
        for group_id in self.event["issue.ids"]:
            group = Group.objects.filter(id=group_id, project=self.event["project.id"]).first()
            if group is None:
                continue

            suspect_spans: list[str] = []
            unique_spans: set[str] = set()
            start: float | None = None
            end: float | None = None
            if light:
                # This value doesn't matter for the light view
                span = [self.event["trace.span"]]
            elif "occurrence_spans" in self.event:
                for problem in self.event["issue_occurrences"]:
                    parent_span_ids = problem.evidence_data.get("parent_span_ids")
                    if parent_span_ids is not None:
                        unique_spans = unique_spans.union(parent_span_ids)
                span = list(unique_spans)
                for event_span in self.event["occurrence_spans"]:
                    for problem in self.event["issue_occurrences"]:
                        offender_span_ids = problem.evidence_data.get("offender_span_ids", [])
                        if event_span.get("span_id") in offender_span_ids:
                            try:
                                end_timestamp = float(event_span.get("timestamp"))
                                if end is None:
                                    end = end_timestamp
                                else:
                                    end = max(end, end_timestamp)
                                if end_timestamp is not None:
                                    start_timestamp = float(
                                        end_timestamp - event_span.get("span.duration")
                                    )
                                    if start is None:
                                        start = start_timestamp
                                    else:
                                        start = min(start, start_timestamp)
                            except ValueError:
                                pass
                            suspect_spans.append(event_span.get("span_id"))
            else:
                if self.nodestore_event is not None or self.span_serialized:
                    occurrence_query = QueryBuilder(
                        Dataset.IssuePlatform,
                        snuba_params,
                        query=f"event_id:{self.event['id']}",
                        selected_columns=["occurrence_id"],
                    )
                    occurrence_ids = occurrence_query.process_results(
                        occurrence_query.run_query("api.trace-view.get-occurrence-ids")
                    )["data"]

                    issue_occurrences = IssueOccurrence.fetch_multi(
                        [occurrence.get("occurrence_id") for occurrence in occurrence_ids],
                        self.event["project.id"],
                    )
                    for problem in issue_occurrences:
                        parent_span_ids = problem.evidence_data.get("parent_span_ids")
                        if parent_span_ids is not None:
                            unique_spans = unique_spans.union(parent_span_ids)
                    span = list(unique_spans)
                    for event_span in self.nodestore_event.data.get("spans", []):
                        for problem in issue_occurrences:
                            offender_span_ids = problem.evidence_data.get("offender_span_ids", [])
                            if event_span.get("span_id") in offender_span_ids:
                                try:
                                    start_timestamp = float(event_span.get("start_timestamp"))
                                    if start is None:
                                        start = start_timestamp
                                    else:
                                        start = min(start, start_timestamp)
                                except ValueError:
                                    pass
                                try:
                                    end_timestamp = float(event_span.get("timestamp"))
                                    if end is None:
                                        end = end_timestamp
                                    else:
                                        end = max(end, end_timestamp)
                                except ValueError:
                                    pass
                                suspect_spans.append(event_span.get("span_id"))
                else:
                    span = [self.event["trace.span"]]

            # Logic for qualified_short_id is copied from property on the Group model
            # to prevent an N+1 query from accessing project.slug everytime
            qualified_short_id = None
            project_slug = self.event["project"]
            if group.short_id is not None:
                qualified_short_id = f"{project_slug.upper()}-{base32_encode(group.short_id)}"

            self.performance_issues.append(
                {
                    "event_id": self.event["id"],
                    "issue_id": group_id,
                    "issue_short_id": qualified_short_id,
                    "span": span,
                    "suspect_spans": suspect_spans,
                    "project_id": self.event["project.id"],
                    "project_slug": self.event["project"],
                    "title": group.title,
                    "level": constants.LOG_LEVELS[group.level],
                    "culprit": group.culprit,
                    "type": group.type,
                    "start": start,
                    "end": end,
                }
            )

    def to_dict(self) -> LightResponse:
        timestamp = datetime.fromisoformat(self.event["timestamp"]).timestamp()
        return {
            "event_id": self.event["id"],
            "span_id": self.event["trace.span"],
            "timestamp": timestamp,
            "transaction": self.event["transaction"],
            "transaction.duration": self.event["transaction.duration"],
            "transaction.op": self.event["transaction.op"],
            "project_id": self.event["project.id"],
            "project_slug": self.event["project"],
            # Avoid empty string for root self.events
            "parent_span_id": self.event["trace.parent_span"] or None,
            "parent_event_id": self.parent_event_id,
            "generation": self.generation,
            "errors": self.errors,
            "performance_issues": self.performance_issues,
        }

    def full_dict(self, detailed: bool = False) -> FullResponse:
        result = cast(FullResponse, self.to_dict())
        if detailed and "transaction.status" in self.event:
            result.update(
                {
                    "transaction.status": SPAN_STATUS_CODE_TO_NAME.get(
                        self.event["transaction.status"], "unknown"
                    ),
                }
            )
        if self.span_serialized:
            result["timestamp"] = datetime.fromisoformat(self.event["timestamp"]).timestamp()
            result["start_timestamp"] = (
                datetime.fromisoformat(self.event["timestamp"]).timestamp()
                - self.event["transaction.duration"]
            )
        if self.nodestore_event:
            result["timestamp"] = self.nodestore_event.data.get("timestamp")
            result["start_timestamp"] = self.nodestore_event.data.get("start_timestamp")

            contexts = self.nodestore_event.data.get("contexts", {})
            profile_id = contexts.get("profile", {}).get("profile_id")
            if profile_id is not None:
                result["profile_id"] = profile_id

            if detailed:
                if "measurements" in self.nodestore_event.data:
                    result["measurements"] = self.nodestore_event.data.get("measurements")
                result["_meta"] = {}
                result["tags"], result["_meta"]["tags"] = get_tags_with_meta(self.nodestore_event)
        # Only add children that have nodestore events, which may be missing if we're pruning for trace navigator
        result["children"] = [
            child.full_dict(detailed) for child in self.children if child.fetched_nodestore
        ]
        return result


def find_timestamp_params(transactions: Sequence[SnubaTransaction]) -> dict[str, datetime | None]:
    min_timestamp = None
    max_timestamp = None
    if transactions:
        first_timestamp = datetime.fromisoformat(transactions[0]["timestamp"])
        min_timestamp = first_timestamp
        max_timestamp = first_timestamp
        for transaction in transactions[1:]:
            timestamp = datetime.fromisoformat(transaction["timestamp"])
            if timestamp < min_timestamp:
                min_timestamp = timestamp
            elif timestamp > max_timestamp:
                max_timestamp = timestamp
    return {
        "min": min_timestamp,
        "max": max_timestamp,
    }


def find_event(
    items: Iterable[_T | None],
    function: Callable[[_T | None], Any],
    default: _T | None = None,
) -> _T | None:
    return next(filter(function, items), default)


def is_root(item: SnubaTransaction) -> bool:
    return item.get("root", "0") == "1"


def child_sort_key(item: TraceEvent) -> list[int]:
    if item.fetched_nodestore and item.nodestore_event is not None:
        return [
            item.nodestore_event.data["start_timestamp"],
            item.nodestore_event.data["timestamp"],
        ]
    else:
        return [
            item.event["transaction"],
            item.event["id"],
        ]


def count_performance_issues(trace_id: str, params: Mapping[str, str]) -> int:
    transaction_query = QueryBuilder(
        Dataset.IssuePlatform,
        params,
        query=f"trace:{trace_id}",
        selected_columns=[],
        limit=MAX_TRACE_SIZE,
    )
    transaction_query.columns.append(Function("count()", alias="total_groups"))
    count = transaction_query.run_query("api.trace-view.count-performance-issues")
    return cast(int, count["data"][0].get("total_groups", 0))


def query_trace_data(
    trace_id: str, params: Mapping[str, str], limit: int
) -> tuple[Sequence[SnubaTransaction], Sequence[SnubaError]]:
    transaction_query = QueryBuilder(
        Dataset.Transactions,
        params,
        query=f"trace:{trace_id}",
        selected_columns=[
            "id",
            "transaction.status",
            "transaction.op",
            "transaction.duration",
            "transaction",
            "timestamp",
            "project",
            "project.id",
            "trace.span",
            "trace.parent_span",
            'to_other(trace.parent_span, "", 0, 1) AS root',
        ],
        # We want to guarantee at least getting the root, and hopefully events near it with timestamp
        # id is just for consistent results
        orderby=["-root", "timestamp", "id"],
        limit=limit,
    )
    occurrence_query = QueryBuilder(
        Dataset.IssuePlatform,
        params,
        query=f"trace:{trace_id}",
        selected_columns=["event_id", "occurrence_id"],
        config=QueryBuilderConfig(
            functions_acl=["groupArray"],
        ),
    )
    occurrence_query.columns.append(
        Function("groupArray", parameters=[Column("group_id")], alias="issue.ids")
    )
    occurrence_query.groupby = [Column("event_id"), Column("occurrence_id")]

    error_query = QueryBuilder(
        Dataset.Events,
        params,
        query=f"trace:{trace_id}",
        selected_columns=[
            "id",
            "project",
            "project.id",
            "timestamp",
            "trace.span",
            "transaction",
            "issue",
            "title",
            "tags[level]",
        ],
        # Don't add timestamp to this orderby as snuba will have to split the time range up and make multiple queries
        orderby=["id"],
        limit=limit,
        config=QueryBuilderConfig(
            auto_fields=False,
        ),
    )
    results = bulk_snql_query(
        [
            transaction_query.get_snql_query(),
            error_query.get_snql_query(),
            occurrence_query.get_snql_query(),
        ],
        referrer="api.trace-view.get-events",
    )

    transformed_results = [
        query.process_results(result)["data"]
        for result, query in zip(results, [transaction_query, error_query, occurrence_query])
    ]

    # Join group IDs from the occurrence dataset to transactions data
    occurrence_issue_ids = {row["event_id"]: row["issue.ids"] for row in transformed_results[2]}
    occurrence_ids = {row["event_id"]: row["occurrence_id"] for row in transformed_results[2]}
    for result in transformed_results[0]:
        result["issue.ids"] = occurrence_issue_ids.get(result["id"], {})
        result["occurrence_id"] = occurrence_ids.get(result["id"])
        result["trace.parent_transaction"] = None

    return cast(Sequence[SnubaTransaction], transformed_results[0]), cast(
        Sequence[SnubaError], transformed_results[1]
    )


def augment_transactions_with_spans(
    transactions: Sequence[SnubaTransaction],
    errors: Sequence[SnubaError],
    trace_id: str,
    params: Mapping[str, str],
) -> Sequence[SnubaTransaction]:
    """Augment the list of transactions with parent, error and problem data"""
    trace_parent_spans = set()  # parent span ids of segment spans
    transaction_problem_map = {}
    problem_project_map = {}
    issue_occurrences = []
    occurrence_spans = set()
    error_spans = {e["trace.span"] for e in errors if e["trace.span"]}
    projects = {e["project.id"] for e in errors if e["trace.span"]}
    ts_params = find_timestamp_params(transactions)
    if ts_params["min"]:
        params["start"] = ts_params["min"] - timedelta(hours=1)
    if ts_params["max"]:
        params["end"] = ts_params["max"] + timedelta(hours=1)

    for transaction in transactions:
        transaction["occurrence_spans"] = []
        transaction["issue_occurrences"] = []

        project = transaction["project.id"]
        projects.add(project)

        # Pull out occurrence data
        transaction_problem_map[transaction["id"]] = transaction
        if project not in problem_project_map:
            problem_project_map[project] = []
        problem_project_map[project].append(transaction["occurrence_id"])

        # Need to strip the leading "0"s to match our query to the spans table
        # This is cause spans are stored as UInt64, so a span like 0011
        # converted to an int then converted to a hex will become 11
        # so when we query snuba we need to remove the 00s ourselves as well
        if not transaction["trace.parent_span"]:
            continue
        transaction["trace.parent_span.stripped"] = (
            str(hex(int(transaction["trace.parent_span"], 16))).lstrip("0x")
            if transaction["trace.parent_span"].startswith("00")
            else transaction["trace.parent_span"]
        )
        # parent span ids of the segment spans
        trace_parent_spans.add(transaction["trace.parent_span.stripped"])

    for project, occurrences in problem_project_map.items():
        if occurrences:
            issue_occurrences.extend(
                [
                    occurrence
                    for occurrence in IssueOccurrence.fetch_multi(occurrences, project)
                    if occurrence is not None
                ]
            )

    for problem in issue_occurrences:
        occurrence_spans = occurrence_spans.union(set(problem.evidence_data["offender_span_ids"]))

    query_spans = {*trace_parent_spans, *error_spans, *occurrence_spans}
    if "" in query_spans:
        query_spans.remove("")
    # If there are no spans to query just return transactions as is
    if len(query_spans) == 0:
        return transactions

    # Fetch parent span ids of segment spans and their corresponding
    # transaction id so we can link parent/child transactions in
    # a trace.
    spans_params = params.copy()
    spans_params["project_objects"] = [p for p in params["project_objects"] if p.id in projects]
    spans_params["project_id"] = list(projects.union(set(problem_project_map.keys())))

    parents_results = SpansIndexedQueryBuilder(
        Dataset.SpansIndexed,
        spans_params,
        query=f"trace:{trace_id} span_id:[{','.join(query_spans)}]",
        selected_columns=[
            "transaction.id",
            "span_id",
            "timestamp",
        ],
        orderby=["timestamp", "id"],
        limit=10000,
    ).run_query(referrer=Referrer.API_TRACE_VIEW_GET_PARENTS.value)

    parent_map = {parent["span_id"]: parent for parent in parents_results["data"]}
    for transaction in transactions:
        # For a given transaction, if parent span id exists in the tranaction (so this is
        # not a root span), see if the indexed spans data can tell us what the parent
        # transaction id is.
        if "trace.parent_span.stripped" in transaction:
            if parent := parent_map.get(transaction["trace.parent_span.stripped"]):
                transaction["trace.parent_transaction"] = parent["transaction.id"]
    for problem in issue_occurrences:
        for span_id in problem.evidence_data["offender_span_ids"]:
            if parent := parent_map.get(span_id):
                transaction = transaction_problem_map[problem.event_id]
                transaction["occurrence_spans"].append(parent)
                transaction["issue_occurrences"].append(problem)
    for error in errors:
        if parent := parent_map.get(error["trace.span"]):
            error["trace.transaction"] = parent["transaction.id"]
    return transactions


class OrganizationEventsTraceEndpointBase(OrganizationEventsV2EndpointBase):
    publish_status = {
        "GET": ApiPublishStatus.PRIVATE,
    }

    def has_feature(self, organization: Organization, request: HttpRequest) -> bool:
        return bool(
            features.has("organizations:performance-view", organization, actor=request.user)
        )

    @staticmethod
    def serialize_error(event: SnubaError) -> TraceError:
        return {
            "event_id": event["id"],
            "issue_id": event["issue.id"],
            "span": event["trace.span"],
            "project_id": event["project.id"],
            "project_slug": event["project"],
            "title": event["title"],
            "level": event["tags[level]"],
            "timestamp": to_timestamp_from_iso_format(event["timestamp"]),
            "event_type": "error",
            "generation": 0,
        }

    @staticmethod
    def construct_parent_map(
        events: Sequence[SnubaTransaction],
    ) -> dict[str, list[SnubaTransaction]]:
        """A mapping of span ids to their transactions

        - Transactions are associated to each other via parent_span_id
        """
        parent_map: dict[str, list[SnubaTransaction]] = defaultdict(list)
        for item in events:
            if not is_root(item):
                parent_map[item["trace.parent_span"]].append(item)
        return parent_map

    @staticmethod
    def construct_error_map(events: Sequence[SnubaError]) -> dict[str, list[SnubaError]]:
        """A mapping of span ids to their errors

        key depends on the event type:
        - Errors are associated to transactions via span_id
        """
        parent_map: dict[str, list[SnubaError]] = defaultdict(list)
        for item in events:
            parent_map[item["trace.span"]].append(item)
        return parent_map

    @staticmethod
    def record_analytics(
        transactions: Sequence[SnubaTransaction], trace_id: str, user_id: int, org_id: int
    ) -> None:
        with sentry_sdk.start_span(op="recording.analytics"):
            len_transactions = len(transactions)

            sentry_sdk.set_tag("trace_view.trace", trace_id)
            sentry_sdk.set_tag("trace_view.transactions", len_transactions)
            sentry_sdk.set_tag(
                "trace_view.transactions.grouped", format_grouped_length(len_transactions)
            )
            set_measurement("trace_view.transactions", len_transactions)
            projects: set[int] = set()
            for transaction in transactions:
                projects.add(transaction["project.id"])

            len_projects = len(projects)
            sentry_sdk.set_tag("trace_view.projects", len_projects)
            sentry_sdk.set_tag("trace_view.projects.grouped", format_grouped_length(len_projects))
            set_measurement("trace_view.projects", len_projects)

    def get(self, request: HttpRequest, organization: Organization, trace_id: str) -> HttpResponse:
        if not self.has_feature(organization, request):
            return Response(status=404)

        try:
            # The trace view isn't useful without global views, so skipping the check here
            params = self.get_snuba_params(request, organization, check_global_views=False)
        except NoProjects:
            return Response(status=404)

        trace_view_load_more_enabled = features.has(
            "organizations:trace-view-load-more",
            organization,
            actor=request.user,
        )

        # Detailed is deprecated now that we want to use spans instead
        detailed: bool = request.GET.get("detailed", "0") == "1"
        use_spans: bool = request.GET.get("useSpans", "0") == "1"
        # Temporary for debugging
        augment_only: bool = request.GET.get("augmentOnly", "0") == "1"
        if detailed and use_spans:
            raise ParseError("Cannot return a detailed response while using spans")
        limit: int = (
            min(int(request.GET.get("limit", MAX_TRACE_SIZE)), 2000)
            if trace_view_load_more_enabled
            else MAX_TRACE_SIZE
        )
        event_id: str | None = request.GET.get("event_id")

        # Only need to validate event_id as trace_id is validated in the URL
        if event_id and not is_event_id(event_id):
            return Response({"detail": INVALID_ID_DETAILS.format("Event ID")}, status=400)

        tracing_without_performance_enabled = features.has(
            "organizations:performance-tracing-without-performance",
            organization,
            actor=request.user,
        )
        with handle_query_errors():
            transactions, errors = query_trace_data(trace_id, params, limit)
            if use_spans or augment_only:
                try:
                    transactions = augment_transactions_with_spans(
                        transactions, errors, trace_id, params
                    )
                except Exception as err:
                    sentry_sdk.capture_exception(err)
                    raise ParseError(detail="augment error")
            if len(transactions) == 0 and not tracing_without_performance_enabled:
                return Response(status=404)
            self.record_analytics(transactions, trace_id, self.request.user.id, organization.id)

        warning_extra: dict[str, str] = {"trace": trace_id, "organization": organization.slug}

        # Look for all root transactions in the trace (i.e., transactions
        # that explicitly have no parent span id)
        roots: list[SnubaTransaction] = []
        for item in transactions:
            if is_root(item):
                roots.append(item)
            else:
                # This is okay because the query does an order by on -root
                break
        if len(roots) > 1:
            sentry_sdk.set_tag("discover.trace-view.warning", "root.extra-found")
            logger.warning(
                "discover.trace-view.root.extra-found",
                extra={"extra_roots": len(roots), **warning_extra},
            )

        return Response(
            self.serialize(
                limit,
                transactions,
                errors,
                roots,
                warning_extra,
                event_id,
                detailed,
                tracing_without_performance_enabled,
                trace_view_load_more_enabled,
                use_spans,
            )
        )


@region_silo_endpoint
class OrganizationEventsTraceLightEndpoint(OrganizationEventsTraceEndpointBase):
    publish_status = {
        "GET": ApiPublishStatus.PRIVATE,
    }

    @staticmethod
    def get_current_transaction(
        transactions: Sequence[SnubaTransaction],
        errors: Sequence[SnubaError],
        event_id: str,
        allow_orphan_errors: bool,
    ) -> tuple[SnubaTransaction, Event]:
        """Given an event_id return the related transaction event

        The event_id could be for an error, since we show the quick-trace
        for both event types
        We occasionally have to get the nodestore data, so this function returns
        the nodestore event as well so that we're doing that in one location.
        """
        transaction_event = find_event(
            transactions, lambda item: item is not None and item["id"] == event_id
        )
        if transaction_event is not None:
            return transaction_event, eventstore.backend.get_event_by_id(
                transaction_event["project.id"], transaction_event["id"]
            )

        # The event couldn't be found, it might be an error
        error_event = find_event(errors, lambda item: item is not None and item["id"] == event_id)
        # Alright so we're looking at an error, time to see if we can find its transaction
        if error_event is not None:
            # Unfortunately the only association from an event back to its transaction is name & span_id
            # First maybe we got lucky and the error happened on the transaction's "span"
            error_span = error_event["trace.span"]
            transaction_event = find_event(
                transactions, lambda item: item is not None and item["trace.span"] == error_span
            )
            if transaction_event is not None:
                return transaction_event, eventstore.backend.get_event_by_id(
                    transaction_event["project.id"], transaction_event["id"]
                )
            # We didn't get lucky, time to talk to nodestore...
            for transaction_event in transactions:
                if transaction_event["transaction"] != error_event["transaction"]:
                    continue

                nodestore_event = eventstore.backend.get_event_by_id(
                    transaction_event["project.id"], transaction_event["id"]
                )
                transaction_spans: NodeSpans = nodestore_event.data.get("spans", [])
                for span in transaction_spans:
                    if span["span_id"] == error_event["trace.span"]:
                        return transaction_event, nodestore_event

        if allow_orphan_errors:
            return None, None

        # The current event couldn't be found in errors or transactions
        raise Http404()

    def serialize(
        self,
        limit: int,
        transactions: Sequence[SnubaTransaction],
        errors: Sequence[SnubaError],
        roots: Sequence[SnubaTransaction],
        warning_extra: dict[str, str],
        event_id: str | None,
        detailed: bool = False,
        allow_orphan_errors: bool = False,
        allow_load_more: bool = False,
        use_spans: bool = False,
    ) -> Sequence[LightResponse]:
        """Because the light endpoint could potentially have gaps between root and event we return a flattened list"""
        if use_spans:
            raise ParseError(detail="useSpans isn't supported on the trace-light")
        if event_id is None:
            raise ParseError(detail="An event_id is required for the light trace")
        snuba_event, nodestore_event = self.get_current_transaction(
            transactions, errors, event_id, allow_orphan_errors
        )
        parent_map = self.construct_parent_map(transactions)
        error_map = self.construct_error_map(errors)
        trace_results: list[TraceEvent] = []
        current_generation: int | None = None
        root_id: str | None = None

        with sentry_sdk.start_span(op="building.trace", description="light trace"):
            # Check if the event is an orphan_error
            if not snuba_event and not nodestore_event and allow_orphan_errors:
                orphan_error = find_event(
                    errors, lambda item: item is not None and item["id"] == event_id
                )
                if orphan_error:
                    return {
                        "transactions": [],
                        "orphan_errors": [self.serialize_error(orphan_error)],
                    }
                else:
                    # The current event couldn't be found in errors or transactions
                    raise Http404()

            # Going to nodestore is more expensive than looping twice so check if we're on the root first
            for root in roots:
                if root["id"] == snuba_event["id"]:
                    current_generation = 0
                    break

            params = self.get_snuba_params(
                self.request, self.request.organization, check_global_views=False
            )
            if current_generation is None:
                for root in roots:
                    # We might not be necessarily connected to the root if we're on an orphan event
                    if root["id"] != snuba_event["id"]:
                        # Get the root event and see if the current event's span is in the root event
                        root_event = eventstore.backend.get_event_by_id(
                            root["project.id"], root["id"]
                        )
                        root_spans: NodeSpans = root_event.data.get("spans", [])
                        root_span = find_event(
                            root_spans,
                            lambda item: item is not None
                            and item["span_id"] == snuba_event["trace.parent_span"],
                        )

                        # We only know to add the root if its the direct parent
                        if root_span is not None:
                            # For the light response, the parent will be unknown unless it is a direct descendent of the root
                            root_id = root["id"]
                            trace_results.append(
                                TraceEvent(
                                    root,
                                    None,
                                    0,
                                    True,
                                    snuba_params=params,
                                )
                            )
                            current_generation = 1
                            break

            current_event = TraceEvent(
                snuba_event, root_id, current_generation, True, snuba_params=params
            )
            trace_results.append(current_event)

            spans: NodeSpans = nodestore_event.data.get("spans", [])
            # Need to include the transaction as a span as well
            #
            # Important that we left pad the span id with 0s because
            # the span id is stored as an UInt64 and converted into
            # a hex string when quering. However, the conversion does
            # not ensure that the final span id is 16 chars long since
            # it's a naive base 10 to base 16 conversion.
            spans.append({"span_id": snuba_event["trace.span"].rjust(16, "0")})

            for span in spans:
                if span["span_id"] in error_map:
                    current_event.errors.extend(
                        [self.serialize_error(error) for error in error_map.pop(span["span_id"])]
                    )
                if span["span_id"] in parent_map:
                    child_events = parent_map.pop(span["span_id"])
                    trace_results.extend(
                        [
                            TraceEvent(
                                child_event,
                                snuba_event["id"],
                                (
                                    current_event.generation + 1
                                    if current_event.generation is not None
                                    else None
                                ),
                                True,
                                snuba_params=params,
                            )
                            for child_event in child_events
                        ]
                    )

        if allow_orphan_errors:
            return {
                "transactions": [result.to_dict() for result in trace_results],
                "orphan_errors": [],
            }

        return [result.to_dict() for result in trace_results]


@region_silo_endpoint
class OrganizationEventsTraceEndpoint(OrganizationEventsTraceEndpointBase):
    @staticmethod
    def update_children(event: TraceEvent, limit: int) -> None:
        """Updates the children of subtraces

        - Generation could be incorrect from orphans where we've had to reconnect back to an orphan event that's
          already been encountered
        - Sorting children events by timestamp
        """
        parents = [event]
        iteration = 0
        while parents and iteration < limit:
            iteration += 1
            parent = parents.pop()
            parent.children.sort(key=child_sort_key)
            for child in parent.children:
                child.generation = parent.generation + 1 if parent.generation is not None else None
                parents.append(child)

    # Concurrently fetches nodestore data to construct and return a dict mapping eventid of a txn
    # to the associated nodestore event.
    @staticmethod
    def nodestore_event_map(events: Sequence[SnubaTransaction]) -> dict[str, Event | None]:
        map = {}
        with ThreadPoolExecutor(max_workers=20) as executor:
            future_to_event = {
                executor.submit(
                    eventstore.backend.get_event_by_id, event["project.id"], event["id"]
                ): event
                for event in events
            }

            for future in as_completed(future_to_event):
                event_id = future_to_event[future]["id"]
                nodestore_event = future.result()
                map[event_id] = nodestore_event

        return map

    def serialize(
        self,
        limit: int,
        transactions: Sequence[SnubaTransaction],
        errors: Sequence[SnubaError],
        roots: Sequence[SnubaTransaction],
        warning_extra: dict[str, str],
        event_id: str | None,
        detailed: bool = False,
        allow_orphan_errors: bool = False,
        allow_load_more: bool = False,
        use_spans: bool = False,
    ) -> Sequence[FullResponse]:
        """For the full event trace, we return the results as a graph instead of a flattened list

        if event_id is passed, we prune any potential branches of the trace to make as few nodestore calls as
        possible
        """
        if use_spans:
            results = self.serialize_with_spans(
                limit,
                transactions,
                errors,
                roots,
                warning_extra,
                event_id,
                detailed,
                allow_orphan_errors,
                allow_load_more,
            )
            return results
        event_id_to_nodestore_event = (
            self.nodestore_event_map(transactions) if allow_load_more else {}
        )
        parent_map = self.construct_parent_map(transactions)
        error_map = self.construct_error_map(errors)
        parent_events: dict[str, TraceEvent] = {}
        results_map: dict[str | None, list[TraceEvent]] = defaultdict(list)
        to_check: Deque[SnubaTransaction] = deque()
        params = self.get_snuba_params(
            self.request, self.request.organization, check_global_views=False
        )
        # The root of the orphan tree we're currently navigating through
        orphan_root: SnubaTransaction | None = None
        if roots:
            results_map[None] = []
        for root in roots:
            root_event = TraceEvent(root, None, 0, snuba_params=params)
            parent_events[root["id"]] = root_event
            results_map[None].append(root_event)
            to_check.append(root)

        iteration = 0
        with sentry_sdk.start_span(op="building.trace", description="full trace"):
            has_orphans = False

            while parent_map or to_check:
                if len(to_check) == 0:
                    has_orphans = True
                    # Grab any set of events from the parent map
                    parent_span_id, current_events = parent_map.popitem()

                    current_event, *siblings = current_events
                    # If there were any siblings put them back
                    if siblings:
                        parent_map[parent_span_id] = siblings

                    previous_event = parent_events[current_event["id"]] = TraceEvent(
                        current_event, None, 0, snuba_params=params
                    )

                    # Used to avoid removing the orphan from results entirely if we loop
                    orphan_root = current_event
                    results_map[parent_span_id].append(previous_event)
                else:
                    current_event = to_check.popleft()
                    previous_event = parent_events[current_event["id"]]

                # We've found the event for the trace navigator so we can remove everything in the deque
                # As they're unrelated ancestors now
                if event_id and current_event["id"] == event_id:
                    # Remove any remaining events so we don't think they're orphans
                    while to_check:
                        to_remove = to_check.popleft()
                        if to_remove["trace.parent_span"] in parent_map:
                            del parent_map[to_remove["trace.parent_span"]]
                    to_check = deque()

                spans: NodeSpans = []
                if allow_load_more:
                    previous_event_id = previous_event.event["id"]
                    if previous_event_id in event_id_to_nodestore_event:
                        previous_event.fetched_nodestore = True
                        nodestore_event = event_id_to_nodestore_event[previous_event_id]
                        previous_event._nodestore_event = nodestore_event
                        spans = nodestore_event.data.get("spans", [])
                else:
                    if previous_event.nodestore_event:
                        spans = previous_event.nodestore_event.data.get("spans", [])

                # Need to include the transaction as a span as well
                #
                # Important that we left pad the span id with 0s because
                # the span id is stored as an UInt64 and converted into
                # a hex string when quering. However, the conversion does
                # not ensure that the final span id is 16 chars long since
                # it's a naive base 10 to base 16 conversion.
                spans.append({"span_id": previous_event.event["trace.span"].rjust(16, "0")})

                for child in spans:
                    if child["span_id"] in error_map:
                        previous_event.errors.extend(
                            [
                                self.serialize_error(error)
                                for error in error_map.pop(child["span_id"])
                            ]
                        )
                    # We need to connect back to an existing orphan trace
                    if (
                        has_orphans
                        and
                        # The child event has already been checked
                        child["span_id"] in results_map
                        and orphan_root is not None
                        and
                        # In the case of a span loop popping the current root removes the orphan subtrace
                        child["span_id"] != orphan_root["trace.parent_span"]
                    ):
                        orphan_subtraces = results_map.pop(child["span_id"])
                        for orphan_subtrace in orphan_subtraces:
                            orphan_subtrace.parent_event_id = previous_event.event["id"]
                        previous_event.children.extend(orphan_subtraces)
                    if child["span_id"] not in parent_map:
                        continue
                    # Avoid potential span loops by popping, so we don't traverse the same nodes twice
                    child_events = parent_map.pop(child["span_id"])

                    for child_event in child_events:
                        parent_events[child_event["id"]] = TraceEvent(
                            child_event,
                            current_event["id"],
                            previous_event.generation + 1
                            if previous_event.generation is not None
                            else None,
                            snuba_params=params,
                        )
                        # Add this event to its parent's children
                        previous_event.children.append(parent_events[child_event["id"]])

                        to_check.append(child_event)
                # Limit iterations just to be safe
                iteration += 1
                if iteration > limit:
                    sentry_sdk.set_tag("discover.trace-view.warning", "surpassed-trace-limit")
                    logger.warning(
                        "discover.trace-view.surpassed-trace-limit",
                        extra=warning_extra,
                    )
                    break

        # We are now left with orphan errors in the error_map,
        # that we need to serialize and return with our results.
        orphan_errors: list[TraceError] = []
        if allow_orphan_errors and iteration < limit:
            for errors in error_map.values():
                for error in errors:
                    orphan_errors.append(self.serialize_error(error))
                    iteration += 1
                    if iteration > limit:
                        break
                if iteration > limit:
                    break

        trace_roots: list[TraceEvent] = []
        orphans: list[TraceEvent] = []
        for index, result in enumerate(results_map.values()):
            for subtrace in result:
                self.update_children(subtrace, limit)
            if index > 0 or len(roots) == 0:
                orphans.extend(result)
            elif len(roots) > 0:
                trace_roots = result
        # We sort orphans and roots separately because we always want the root(s) as the first element(s)
        trace_roots.sort(key=child_sort_key)
        orphans.sort(key=child_sort_key)
        orphan_errors = sorted(orphan_errors, key=lambda k: k["timestamp"])

        if len(orphans) > 0:
            sentry_sdk.set_tag("discover.trace-view.contains-orphans", "yes")
            logger.warning("discover.trace-view.contains-orphans", extra=warning_extra)

        if allow_orphan_errors:
            return {
                "transactions": [trace.full_dict(detailed) for trace in trace_roots]
                + [orphan.full_dict(detailed) for orphan in orphans],
                "orphan_errors": [orphan for orphan in orphan_errors],
            }

        return (
            [trace.full_dict(detailed) for trace in trace_roots]
            + [orphan.full_dict(detailed) for orphan in orphans]
            + [orphan for orphan in orphan_errors]
        )

    def serialize_with_spans(
        self,
        limit: int,
        transactions: Sequence[SnubaTransaction],
        errors: Sequence[SnubaError],
        roots: Sequence[SnubaTransaction],
        warning_extra: dict[str, str],
        event_id: str | None,
        detailed: bool = False,
        allow_orphan_errors: bool = False,
        allow_load_more: bool = False,
    ) -> Sequence[FullResponse]:
        root_traces: list[TraceEvent] = []
        orphans: list[TraceEvent] = []
        visited_transactions: set[str] = set()
        visited_errors: set[str] = set()
        if not allow_orphan_errors:
            raise ParseError("Must allow orphan errors to useSpans")
        if detailed:
            raise ParseError("Cannot return a detailed response using Spans")

        # A trace can have multiple roots, so we want to visit
        # all roots in a trace and build their children.
        # A root segment is one that doesn't have a parent span id
        # but here is identified by the attribute "root" = 1 on
        # a SnubaTransaction object.
        root_traces = self.visit_transactions(
            roots,
            transactions,
            errors,
            visited_transactions,
            visited_errors,
        )

        # At this point all the roots have their tree built. Remaining
        # transactions are either orphan transactions or children of
        # orphan transactions. Orphan transactions (unlike roots) have
        # a parent_id but the parent_id wasn't found (dropped span).
        # We get a sorted list of these transactions by start timestamp.
        remaining_transactions = self.calculate_remaining_transactions(
            transactions, visited_transactions
        )

        # Determine orphan transactions. `trace.parent_transaction` on a
        # transaction is set when the indexed spans dataset has a row for
        # the parent span id for this transaction. Since we already considered
        # the root spans cases, the remaining spans with no parent transaction
        # id are orphan transactions.
        orphan_roots = [
            orphan
            for orphan in remaining_transactions
            if orphan["trace.parent_transaction"] is None
        ]

        # Build the trees for all the orphan transactions.
        orphans = self.visit_transactions(
            orphan_roots,
            remaining_transactions,
            errors,
            visited_transactions,
            visited_errors,
        )

        # Remaining are transactions with parent transactions but those
        # parents don't map to any of the existing transactions.
        remaining_transactions = self.calculate_remaining_transactions(
            transactions, visited_transactions
        )
        orphans.extend(
            self.visit_transactions(
                remaining_transactions,
                remaining_transactions,
                errors,
                visited_transactions,
                visited_errors,
            )
        )

        # Sort the results so they're consistent
        orphan_errors = sorted(
            [error for error in errors if error["id"] not in visited_errors],
            key=lambda k: k["timestamp"],
        )
        root_traces.sort(key=child_sort_key)
        orphans.sort(key=child_sort_key)

        return {
            "transactions": [trace.full_dict(detailed) for trace in root_traces]
            + [orphan.full_dict(detailed) for orphan in orphans],
            "orphan_errors": [self.serialize_error(error) for error in orphan_errors],
        }

    def calculate_remaining_transactions(self, transactions, visited_transactions):
        return sorted(
            [
                transaction
                for transaction in transactions
                if transaction["id"] not in visited_transactions
            ],
            key=lambda k: -datetime.fromisoformat(k["timestamp"]).timestamp(),
        )

    def visit_transactions(
        self, to_visit, transactions, errors, visited_transactions, visited_errors
    ):
        serialized_events: list[TraceEvent] = []
        for transaction in to_visit:
            if transaction["id"] in visited_transactions:
                continue
            visited_transactions.add(transaction["id"])
            root_event = TraceEvent(transaction, None, 0, span_serialized=True)
            self.add_children(
                root_event, transactions, visited_transactions, errors, visited_errors, 1
            )
            serialized_events.append(root_event)
        return serialized_events

    def add_children(
        self, parent, transactions, visited_transactions, errors, visited_errors, generation
    ):
        for error in errors:
            if error["id"] in visited_errors:
                continue
            if "trace.transaction" in error and error["trace.transaction"] == parent.event["id"]:
                visited_errors.add(error["id"])
                parent.errors.append(self.serialize_error(error))

        # Loop through all the transactions to see if any of them are
        # children.
        for transaction in transactions:
            if transaction["id"] in visited_transactions:
                continue
            if transaction["trace.parent_transaction"] == parent.event["id"]:
                # If transaction is a child, establish that relationship and add it
                # to visited_transactions.
                visited_transactions.add(transaction["id"])
                new_child = TraceEvent(
                    transaction, parent.event["id"], generation, span_serialized=True
                )
                # Repeat adding children until there are none.
                self.add_children(
                    new_child,
                    transactions,
                    visited_transactions,
                    errors,
                    visited_errors,
                    generation + 1,
                )
                parent.children.append(new_child)
        parent.children.sort(key=child_sort_key)


@region_silo_endpoint
class OrganizationEventsTraceMetaEndpoint(OrganizationEventsTraceEndpointBase):
    publish_status = {
        "GET": ApiPublishStatus.PRIVATE,
    }

    def get(self, request: HttpRequest, organization: Organization, trace_id: str) -> HttpResponse:
        if not self.has_feature(organization, request):
            return Response(status=404)

        try:
            # The trace meta isn't useful without global views, so skipping the check here
            params = self.get_snuba_params(request, organization, check_global_views=False)
        except NoProjects:
            return Response(status=404)

        with handle_query_errors():
            result = discover.query(
                selected_columns=[
                    "count_unique(project_id) as projects",
                    "count_if(event.type, equals, transaction) as transactions",
                    "count_if(event.type, notEquals, transaction) as errors",
                ],
                params=params,
                query=f"trace:{trace_id}",
                limit=1,
                referrer="api.trace-view.get-meta",
            )
            if len(result["data"]) == 0:
                return Response(status=404)
            # Merge the result back into the first query
            result["data"][0]["performance_issues"] = count_performance_issues(trace_id, params)
        return Response(self.serialize(result["data"][0]))

    @staticmethod
    def serialize(results: Mapping[str, int]) -> Mapping[str, int]:
        return {
            # Values can be null if there's no result
            "projects": results.get("projects") or 0,
            "transactions": results.get("transactions") or 0,
            "errors": results.get("errors") or 0,
            "performance_issues": results.get("performance_issues") or 0,
        }
