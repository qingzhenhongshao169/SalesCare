export function safeCaseUrl(value) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password && !url.search && !url.hash
      ? url.href : null;
  } catch { return null; }
}

export function visibleCaseCards(items) {
  const seen = new Set();
  return (Array.isArray(items) ? items : []).filter(item => {
    if (!item || !item.id || seen.has(item.id) || !safeCaseUrl(item.url)) return false;
    seen.add(item.id);
    return true;
  }).slice(0, 2);
}

export function applyCaseUpdates(messages, updates) {
  if (!Array.isArray(updates)) return messages;
  const byId = new Map(updates.filter(v => v && typeof v.message_id === 'string' && Array.isArray(v.case_recommendations))
    .map(v => [v.message_id, visibleCaseCards(v.case_recommendations)]));
  return messages.map(message => byId.has(message.id) ? {...message, case_recommendations: byId.get(message.id)} : message);
}
