'use strict';
window.SvxCommands = {
  async sendDtmf(nodeId, sequence) {
    const response = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/dtmf-commands`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sequence}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data.command_id;
  },

  async waitForCommand(commandId, successMessage, failureMessage, maxAttempts = 60, pollInterval = 1000, onProgress = null) {
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, pollInterval));
      const response = await fetch(`/api/commands/${encodeURIComponent(commandId)}`, {cache: 'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      if (result.status === 'in_progress' && result.progress?.message && onProgress) {
        onProgress(result.progress);
      }
      if (result.status === 'completed') {
        return typeof successMessage === 'function' ? successMessage(result.result) : successMessage;
      }
      if (result.status === 'failed') throw new Error(result.result?.error || failureMessage);
      if (result.status === 'expired') throw new Error('Åtgärden hann löpa ut innan nodagenten tog emot den.');
    }
    throw new Error('Nodagenten har ännu inte bekräftat åtgärden.');
  },
};
