/** A message deduplication ID, never an authentication credential. */
export function createMessageId(): string {
  const random = globalThis.crypto;
  if (typeof random?.randomUUID === 'function') return random.randomUUID();

  // LAN HTTP origins do not expose randomUUID, but support getRandomValues.
  if (typeof random?.getRandomValues !== 'function') {
    throw new Error('当前浏览器不支持发送消息，请使用新版 Chrome 或 Edge 浏览器。');
  }
  const bytes = random.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
