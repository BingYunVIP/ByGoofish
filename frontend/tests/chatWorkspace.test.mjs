import test from 'node:test';
import assert from 'node:assert/strict';
import { draftKey, mergeMessages, mergeOutgoing, messageTimeline, unread, shipOrderResult, selectConversations, forwardRecipients, confirmedMessage } from '../lib/chatWorkspace.mjs';
test('drafts are scoped to authenticated user, account and cid', () => {
  const keys = [
    draftKey(1, 'shop-a', 'chat'),
    draftKey(2, 'shop-a', 'chat'),
    draftKey(1, 'shop-b', 'chat'),
    draftKey(1, 'shop-a', 'chat-b'),
  ];
  assert.equal(new Set(keys).size, 4);
});
test('history deduplicates platform ids and preserves chronological order', () => {
  const message = (id, time, text = 'fixture') => ({
    messageId: id,
    time,
    text,
    images: [],
    senderId: 'buyer',
    type: 'text',
  });
  const combined = mergeMessages(
    [message('b', 20), message('c', 30)],
    [message('a', 10), message('b', 20, 'updated')]
  );
  assert.deepEqual(
    combined.map((m) => m.messageId),
    ['a', 'b', 'c']
  );
  assert.equal(combined[1].text, 'updated');
});
test('messages without ids retain distinct senders and image payloads', () => {
  const first = {
    messageId: '',
    time: 1,
    senderId: 'buyer',
    text: '',
    images: ['image-a'],
    type: 'image',
  };
  assert.equal(mergeMessages([first], [{ ...first, images: ['image-b'] }]).length, 2);
});
test('workbench read watermark does not hide new content', () => {
  const conv = {
    lastMessageTime: 10,
    lastMessageSummary: 'old',
    unreadCount: 2,
    workspace: { viewed_message_time: 10, viewed_message_summary: 'old', viewed_unread_count: 2 },
  };
  assert.equal(unread(conv), 0);
  assert.equal(unread({ ...conv, lastMessageSummary: 'new' }), 2);
  assert.equal(unread({ ...conv, lastMessageTime: 11 }), 2);
  assert.equal(unread({ ...conv, unreadCount: 3 }), 3);
});
test('transport success cannot substitute for order shipment success', () => {
  assert.equal(
    shipOrderResult({ success: true, results: [{ success: false, message: '待核查' }] }).success,
    false
  );
  assert.equal(shipOrderResult({ success: true }).success, false);
  assert.equal(shipOrderResult({ success: false, results: [{ success: true }] }).success, false);
  assert.equal(
    shipOrderResult({ success: true, results: [{ success: true, message: '完成' }] }).success,
    true
  );
});

const echo = (changes = {}) => ({ messageId: 'request', client_request_id: 'request', workspace_scope: 'shop\0chat',
  text: '即时消息', images: [], isSelf: true, senderId: 'shop', type: 'text', time: 10, send_status: 'sending', ...changes });
test('local text and image echoes appear before platform history and keep statuses', () => {
  const local = [echo(), echo({ messageId: 'image-request', client_request_id: 'image-request', type: 'image',
    text: '', images: ['blob:fixture'], send_status: 'unknown' })];
  assert.deepEqual(messageTimeline([], local), local);
  assert.equal(messageTimeline([], [echo({ send_status: 'failed' })])[0].send_status, 'failed');
});
test('platform message ids replace exactly one local echo without content matching', () => {
  const sent = echo({ messageId: 'official', send_status: 'sent' });
  const official = { ...sent, client_request_id: undefined, text: '官方正文', time: 11 };
  const timeline = messageTimeline([official, { ...official, messageId: 'another' }], [sent]);
  assert.equal(timeline.length, 2);
  assert.equal(timeline.find((m) => m.messageId === 'official').text, '官方正文');
  assert.equal(timeline.find((m) => m.messageId === 'official').client_request_id, 'request');
});
test('context refresh preserves local image and text, scopes records and cannot downgrade sent', () => {
  const local = echo({ images: ['blob:fixture'], type: 'image', send_status: 'sent' });
  const result = mergeOutgoing([local, echo({ workspace_scope: 'other' })], [
    { request_id: 'request', message_id: 'official', kind: 'image', status: 'sending', created_at: 1 },
  ], 'shop\0chat', 'shop');
  assert.equal(result.length, 1);
  assert.deepEqual(result[0].images, ['blob:fixture']);
  assert.equal(result[0].send_status, 'sent');
  assert.equal(result[0].messageId, 'official');
  assert.equal(result[0].time, 10);
});

test('forward and sidebar share dates, search, unread, pending and pin ordering', () => {
  const start = Date.parse('2026-10-23T00:00:00Z'), end = Date.parse('2026-10-30T00:00:00Z');
  const bounds = { startTime: new Date(start).toISOString(), endTime: new Date(end).toISOString() };
  const row = (cid, time, patch = {}) => ({ cid, lastMessageTime: time, otherUserName: 'Buyer',
    otherUserId: cid, itemTitle: 'Product', lastMessageSummary: 'Hello', unreadCount: 1, ...patch });
  const rows = [row('old',start-1),row('boundary',start/1000),row('new',end-1),row('future',end),
    row('pin',start+1,{workspace:{pinned:true,needs_reply:true}}),row('invalid',0)];
  const selected = selectConversations(rows,{bounds});
  assert.deepEqual(selected.map(v=>v.cid),['pin','new','boundary']);
  assert.deepEqual(selectConversations(rows,{bounds,query:'BUYER',filter:'pending'}).map(v=>v.cid),['pin']);
  assert.equal(selectConversations(rows,{bounds,query:'missing'}).length,0);
  const viewed = {...rows[2],workspace:{viewed_message_time:end,viewed_message_summary:'Hello',viewed_unread_count:1}};
  assert.equal(selectConversations([viewed],{bounds,filter:'unread'}).length,0);
  assert.equal(rows[0].cid,'old'); // never mutate the original page
});
test('forward recipients contain only minimum contact fields, not business context', () => {
  const [recipient] = forwardRecipients([{cid:'c',otherUserId:'buyer',otherUserName:'Buyer',
    lastMessageTime:1234,itemTitle:'Item',orders:[{address:'private'}],note:'private',lastMessageSummary:'private'}]);
  assert.deepEqual(Object.keys(recipient).sort(),['cid','itemTitle','lastMessageTime','otherUserId','otherUserName'].sort());
});

test('only real confirmed platform messages permit forwarding after local echo reconciliation', () => {
  const local={messageId:'request',client_request_id:'request',send_status:'sending'};
  assert.equal(confirmedMessage(local),false);
  assert.equal(confirmedMessage({...local,send_status:'sent'}),false);
  assert.equal(confirmedMessage({...local,messageId:'platform',send_status:'unknown'}),false);
  assert.equal(confirmedMessage({...local,messageId:'platform',send_status:'failed'}),false);
  assert.equal(confirmedMessage({...local,messageId:'platform',send_status:'sent'}),true);
  assert.equal(confirmedMessage({messageId:'platform'}),true);
});
