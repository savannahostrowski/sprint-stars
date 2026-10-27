import test from 'node:test';
import assert from 'node:assert/strict';
import {rankMembers, groupActivities, requestCoordinator, validRange, activityLabel} from '../static/app.js';

const member = (login, closures, prs_opened, reviews = 0) => ({login, name: login, closures, prs_opened, reviews, total: closures + prs_opened + reviews});

test('a longer range updates totals and ranks even if the leader stays unchanged', () => {
  const short = rankMembers([member('ambv', 204, 1), member('vstinner', 9, 5), member('ZeroIntensity', 56, 15)], 'total');
  const long = rankMembers([member('ambv', 204, 1), member('vstinner', 88, 58), member('ZeroIntensity', 76, 17)], 'total');
  assert.equal(short.ranked[0].total, long.ranked[0].total);
  assert.equal(short.ranked[1].login, 'ZeroIntensity');
  assert.equal(long.ranked[1].login, 'vstinner');
  assert.equal(long.ranked[1].total, 146);
  assert.equal(short.totals.total, 290);
  assert.equal(long.totals.total, 444);
});

test('all three views rank by their own score, keep ties, and preserve team totals during search', () => {
  const members = [member('alpha', 8, 1), member('bravo', 2, 7), member('charlie', 0, 2)];
  const snapshot = structuredClone(members);
  assert.deepEqual(rankMembers(members, 'total').ranked.map(m => m.rank), [1, 1, 3]);
  assert.equal(rankMembers(members, 'closures').ranked[0].login, 'alpha');
  assert.equal(rankMembers(members, 'prs_opened').ranked[0].login, 'bravo');
  const search = rankMembers(members, 'prs_opened', 'alpha');
  assert.equal(search.filtered[0].rank, 3);
  assert.equal(search.totals.total, 20);
  assert.deepEqual(members, snapshot);
});

test('activity pagination groups opening and closure actions into a single current-status item', () => {
  const events = [
    {number: 1, activity: 'closure', occurred_at: '2026-10-20T10:00:00Z', status: 'merged'},
    {number: 1, activity: 'pr_opened', occurred_at: '2026-10-19T10:00:00Z', status: 'merged'},
    {number: 2, activity: 'closure', occurred_at: '2026-10-21T10:00:00Z', status: 'closed'},
  ];
  const all = groupActivities(events, 'total');
  assert.equal(all.events.length, 3);
  assert.equal(all.groups.length, 2);
  assert.equal(all.groups[0].number, 2);
  assert.deepEqual(all.groups[1].actions.map(a => a.activity), ['pr_opened', 'closure']);
  assert.equal(groupActivities(events, 'prs_opened').groups[0].status, 'merged');
  assert.equal(groupActivities(events, 'closures').groups.length, 2);
});

test('a slow old range cannot overwrite the newer selected range', () => {
  const gate = requestCoordinator();
  const old = gate.begin();
  const latest = gate.begin();
  assert.equal(old.controller.signal.aborted, true);
  assert.equal(gate.current(old), false);
  gate.finish(old);
  assert.equal(gate.current(latest), true);
  gate.finish(latest);
  assert.equal(gate.current(latest), false);
});

test('a background refresh cannot interrupt a user date change', () => {
  const gate = requestCoordinator();
  const refresh = gate.begin(true);
  const user = gate.begin();
  assert.equal(refresh.controller.signal.aborted, true);
  assert.equal(gate.begin(true), null);
  assert.equal(gate.current(user), true);
  gate.finish(user);
  assert.ok(gate.begin(true));
});

test('date ranges accept 1–93 calendar days and reject reversed, invalid, and larger ranges', () => {
  assert.equal(validRange('2026-10-21', '2026-10-21'), true);
  assert.equal(validRange('2026-07-25', '2026-10-25'), true);
  assert.equal(validRange('2026-07-24', '2026-10-25'), false);
  assert.equal(validRange('2026-10-25', '2026-10-12'), false);
  assert.equal(validRange('2026-02-31', '2026-03-05'), false);
  assert.equal(validRange(null, null), false);
});


test('same-number items from different repositories stay separate with their own status', () => {
  const items = [
    {repository: 'acme/widgets', number: 1, activity: 'closure', status: 'merged', occurred_at: '2026-10-20T10:00:00Z'},
    {repository: 'acme/widgets', number: 1, activity: 'pr_opened', status: 'merged', occurred_at: '2026-10-19T10:00:00Z'},
    {repository: 'acme/docs', number: 1, activity: 'pr_opened', status: 'open', occurred_at: '2026-10-21T10:00:00Z'},
  ];
  const {events, groups} = groupActivities(items, 'total');
  assert.equal(events.length, 3);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups.map(i => [i.repository, i.status, i.actions.length]), [
    ['acme/docs', 'open', 1], ['acme/widgets', 'merged', 2],
  ]);
});


test('alternate login search returns the canonical contributor without changing totals', () => {
  const carol = {...member('Carol', 5, 3), aliases: ['carol-work']};
  const view = rankMembers([carol], 'total', 'CAROL-WORK');
  assert.equal(view.filtered.length, 1);
  assert.equal(view.filtered[0].login, 'Carol');
  assert.equal(view.totals.total, 8);
});


test('reviews rank independently, contribute to All, and show only review actions in the tab', () => {
  const members = [member('alice', 2, 0, 3), member('bob', 0, 1, 8)];
  assert.equal(rankMembers(members, 'reviews').ranked[0].login, 'bob');
  assert.equal(rankMembers(members, 'total').totals.total, 14);
  assert.equal(rankMembers(members, 'reviews').totals.reviews, 11);
  const items = [
    {repository: 'acme/widgets', number: 1, activity: 'closure', status: 'merged', occurred_at: '2026-10-21T10:00:00Z'},
    {repository: 'acme/widgets', number: 1, activity: 'review', status: 'merged', occurred_at: '2026-10-20T10:00:00Z'},
  ];
  assert.equal(groupActivities(items, 'total').groups.length, 1);
  assert.equal(groupActivities(items, 'reviews').events.length, 1);
  assert.equal(groupActivities(items, 'reviews').events[0].activity, 'review');
  assert.equal(groupActivities(items, 'closures').events[0].activity, 'closure');
  assert.equal(activityLabel({activity: 'review', review_state: 'APPROVED'}), 'Reviewed (approved)');
  assert.equal(activityLabel({activity: 'review', review_state: 'CHANGES_REQUESTED'}), 'Reviewed (changes requested)');
});
