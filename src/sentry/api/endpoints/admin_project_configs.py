from django.http import Http404
from rest_framework.request import Request
from rest_framework.response import Response

from sentry.api.api_owners import ApiOwner
from sentry.api.api_publish_status import ApiPublishStatus
from sentry.api.base import Endpoint, region_silo_endpoint
from sentry.api.permissions import SuperuserOrStaffFeatureFlaggedPermission
from sentry.models.project import Project
from sentry.relay import projectconfig_cache
from sentry.tasks.relay import schedule_invalidate_project_config


# NOTE: This endpoint should be in getsentry
@region_silo_endpoint
class AdminRelayProjectConfigsEndpoint(Endpoint):
    owner = ApiOwner.OWNERS_INGEST
    publish_status = {
        "GET": ApiPublishStatus.PRIVATE,
        "POST": ApiPublishStatus.PRIVATE,
    }
    permission_classes = (SuperuserOrStaffFeatureFlaggedPermission,)

    def get(self, request: Request) -> Response:
        project_id = request.GET.get("projectId")

        project_keys = []
        if project_id is not None:
            try:
                project = Project.objects.get_from_cache(id=project_id)
                for project_key in project.key_set.all():
                    project_keys.append(project_key.public_key)

            except Exception:
                raise Http404

        project_key_param = request.GET.get("projectKey")
        if project_key_param is not None:
            project_keys.append(project_key_param)

        configs = {}
        for key in project_keys:
            cached_config = projectconfig_cache.backend.get(key)
            if cached_config is not None:
                configs[key] = cached_config
            else:
                configs[key] = None

        # TODO: if we don't think we'll add anything to the endpoint
        # we may as well return just the configs
        return Response({"configs": configs}, status=200)

    def post(self, request: Request) -> Response:
        """Regenerate the project config"""
        project_id = request.data.get("projectId")

        if not project_id:
            return Response({"error": "Missing project id"}, status=400)

        try:
            schedule_invalidate_project_config(
                project_id=project_id, trigger="_admin_trigger_invalidate_project_config"
            )

        except Exception:
            raise Http404

        return Response(status=204)
