import test from 'node:test';
import assert from 'node:assert/strict';
import {safeCaseUrl, visibleCaseCards, applyCaseUpdates} from '../mvp/caseCards.mjs';

test('case cards never render script, credentials or malformed links', () => {
  for (const url of ['javascript:alert(1)', 'data:text/html,abc', '//evil.test', 'http://example.com',
    'https://u:p@example.com/case', 'https://example.com/case?redirect=x', 'https://example.com/#a', null]) {
    assert.equal(safeCaseUrl(url), null);
  }
  assert.equal(safeCaseUrl('https://www.biaofun.com/case/5/26.html'), 'https://www.biaofun.com/case/5/26.html');
});

test('case cards deduplicate and cap attachments without dropping the reply', () => {
  const one = {id: 'a', url: 'https://www.biaofun.com/case/5/26.html'};
  const two = {id: 'b', url: 'https://www.biaofun.com/case/5/21.html'};
  assert.deepEqual(visibleCaseCards([one, one, {id:'bad', url:'javascript:x'}, two, {...two, id:'c'}]), [one, two]);
  assert.deepEqual(visibleCaseCards(null), []);
});

test('SSE revocation removes old cards without changing message text or sequence', () => {
  const messages=[{id:'a', sequence:2, text:'original', case_recommendations:[{id:'case'}]}];
  const changed=applyCaseUpdates(messages,[{message_id:'a',case_recommendations:[],text:'injected',sequence:99}]);
  assert.equal(changed[0].text,'original');assert.equal(changed[0].sequence,2);
  assert.deepEqual(changed[0].case_recommendations,[]);
  assert.equal(messages[0].case_recommendations.length,1);
});

test('case updates cannot add unknown messages and reject malformed events', () => {
  const messages=[{id:'a',sequence:2,text:'original'}];
  assert.deepEqual(applyCaseUpdates(messages,[{message_id:'unknown',case_recommendations:[]}]),messages);
  assert.equal(applyCaseUpdates(messages,null),messages);
});
