import type {MRI} from 'sentry/types/metrics';
import {MetricSeriesFilterUpdateType} from 'sentry/utils/metrics/types';

import {ensureQuotedTextFilters, updateQueryWithSeriesFilter} from './index';

describe('updateQueryWithSeriesFilter', () => {
  it('should add a filter an empty query stirng', () => {
    const query = {
      mri: 'd:transactions/duration@milisecond' as MRI,
      op: 'count',
      groupBy: [],
      query: '',
    };

    const updatedQuery = updateQueryWithSeriesFilter(
      query,
      {project: '1'},
      MetricSeriesFilterUpdateType.ADD
    );

    expect(updatedQuery.query).toEqual('project:"1"');
    expect(updatedQuery.groupBy).toEqual([]);
  });

  it('should add a filter to an existing query string', () => {
    const query = {
      mri: 'd:transactions/duration@milisecond' as MRI,
      op: 'count',
      groupBy: [],
      query: 'release:"latest" AND (environment:production OR environment:staging)',
    };

    const updatedQuery = updateQueryWithSeriesFilter(
      query,
      {project: '1'},
      MetricSeriesFilterUpdateType.ADD
    );

    expect(updatedQuery.query).toEqual(
      '( release:latest AND ( environment:production OR environment:staging ) ) project:"1"'
    );
    expect(updatedQuery.groupBy).toEqual([]);
  });

  it('should exclude a filter from an empty query string', () => {
    const query = {
      mri: 'd:transactions/duration@milisecond' as MRI,
      op: 'count',
      groupBy: [],
      query: '',
    };

    const updatedQuery = updateQueryWithSeriesFilter(
      query,
      {project: '1'},
      MetricSeriesFilterUpdateType.EXCLUDE
    );

    expect(updatedQuery.query).toEqual('!project:"1"');
    expect(updatedQuery.groupBy).toEqual([]);
  });

  it('should exclude a filter from an existing query string', () => {
    const query = {
      mri: 'd:transactions/duration@milisecond' as MRI,
      op: 'count',
      groupBy: [],
      query: 'environment:prod1 OR environment:prod2',
    };

    const updatedQuery = updateQueryWithSeriesFilter(
      query,
      {project: '2'},
      MetricSeriesFilterUpdateType.EXCLUDE
    );

    expect(updatedQuery.query).toEqual(
      '( environment:prod1 OR environment:prod2 ) !project:"2"'
    );
    expect(updatedQuery.groupBy).toEqual([]);
  });

  it('should add multiple filters and remove them from grouping', () => {
    const query = {
      mri: 'd:transactions/duration@milisecond' as MRI,
      op: 'count',
      groupBy: ['project', 'release', 'environment'],
      query: '',
    };

    const updatedQuery = updateQueryWithSeriesFilter(
      query,
      {project: '1', release: 'latest'},
      MetricSeriesFilterUpdateType.ADD
    );

    expect(updatedQuery.query).toEqual('project:"1" release:"latest"');
    expect(updatedQuery.groupBy).toEqual(['environment']);
  });
});

describe('ensureQuotedTextFilters', () => {
  it('returns a query with all text filters quoted', () => {
    expect(ensureQuotedTextFilters('transaction:/{organization_slug}/')).toEqual(
      'transaction:"/{organization_slug}/"'
    );

    // transaction.duration defaults to a number filter
    expect(ensureQuotedTextFilters('transaction.duration:100')).toEqual(
      'transaction.duration:100'
    );
  });

  it('handles negated filters', () => {
    expect(ensureQuotedTextFilters('!transaction:/{organization_slug}/')).toEqual(
      '!transaction:"/{organization_slug}/"'
    );

    // transaction.duration defaults to a number filter
    expect(ensureQuotedTextFilters('!transaction.duration:100')).toEqual(
      '!transaction.duration:100'
    );
  });

  it('applies config overrides', () => {
    expect(
      ensureQuotedTextFilters('transaction:100', {
        numericKeys: new Set(['transaction']),
      })
    ).toEqual('transaction:100');

    expect(
      ensureQuotedTextFilters('transaction.duration:100', {
        numericKeys: new Set([]),
      })
    ).toEqual('transaction.duration:"100"');
  });
});
