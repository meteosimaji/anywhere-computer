// Observe one owned generation response. IDs are candidates, never acceptance.
function observeSubchatStream(binding) {
  const original = window.fetch;
  let active = true;
  const wrapped = async function (...args) {
    const response = await Reflect.apply(original, this, args);
    if (!active || response.url !== 'https://chatgpt.com/backend-api/f/conversation' ||
        !response.ok || !response.headers.get('content-type')?.includes('text/event-stream')) return response;
    let input;
    try {
      const body = JSON.parse(args[1]?.body);
      if (body.messages?.length !== 1) return response;
      input = body.messages[0].id;
      if (typeof input !== 'string' || !input || input.length > 256) return response;
    } catch { return response; }
    active = false;
    if (window.fetch === wrapped) window.fetch = original;
    const reader = response.clone().body?.getReader();
    if (!reader) return response;
    const timer = setTimeout(() => { void reader.cancel().catch(() => {}); }, 60000);
    void (async () => {
      const decoder = new TextDecoder();
      let bytes = 0, buffer = '';
      try {
        while (true) {
          const {done, value} = await reader.read();
          if (done) break;
          bytes += value.byteLength;
          if (bytes > 4 * 1024 * 1024) break;
          buffer += decoder.decode(value, {stream: true});
          let separator;
          while ((separator = /\r?\n\r?\n/.exec(buffer))) {
            const frame = buffer.slice(0, separator.index);
            buffer = buffer.slice(separator.index + separator[0].length);
            if (frame.length > 512 * 1024) return;
            let event;
            try {
              event = JSON.parse(frame.split(/\r?\n/).filter(line => line.startsWith('data:'))
                .map(line => line.slice(5).trimStart()).join('\n'));
            } catch { continue; }
            // Accept only a full root event, not arbitrary nested text or a patch.
            const root = event?.p === '' && ['add', 'replace'].includes(event.o) ? event.v : event;
            const id = root?.conversation_id;
            if (typeof id !== 'string' || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(id)) continue;
            await window[binding](input, id);
            return;
          }
          if (buffer.length > 512 * 1024) break;
        }
      } catch { /* Missing evidence never authorizes a replay. */ }
      finally { clearTimeout(timer); void reader.cancel().catch(() => {}); }
    })();
    return response;
  };
  window.fetch = wrapped;
  setTimeout(() => { active = false; if (window.fetch === wrapped) window.fetch = original; }, 60000);
  // Cleanup must not overwrite another observer installed by the application.
  return () => { active = false; if (window.fetch === wrapped) window.fetch = original; };
}
