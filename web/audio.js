(() => {
  const WIRE_RATE = 16000;
  const MAX_BUFFERED_SECONDS = 0.6;
  const START_DELAY_SECONDS = 0.15;
  const panel = document.getElementById('audio-panel');
  if (!panel) return;
  const nodeId = window.location.pathname.split('/').filter(Boolean).pop();
  const base = `/api/nodes/${encodeURIComponent(nodeId)}`;
  const connectButton = document.getElementById('audio-connect');
  const pttButton = document.getElementById('audio-ptt');
  const volume = document.getElementById('audio-volume');
  const stateText = document.getElementById('audio-state');
  const errorText = document.getElementById('audio-error');
  let capabilities = {listen: false, talk: false, ptt: false, issues: [], error: ''};
  let nodeOnline = false;
  let connected = false;
  let abort = null;
  let context = null;
  let gain = null;
  let playAt = 0;
  let mic = null;
  let pttHeld = false;
  let pttSending = false;
  let sendChain = Promise.resolve();
  let pendingSends = 0;

  const setState = (text) => { stateText.textContent = text; };
  const setError = (text) => { errorText.textContent = text || ''; };

  function render() {
    panel.hidden = !(capabilities.listen || capabilities.talk);
    connectButton.disabled = !capabilities.listen || !nodeOnline;
    connectButton.textContent = connected ? 'Koppla från ljud' : 'Anslut ljud';
    pttButton.disabled = !capabilities.talk || !nodeOnline;
    pttButton.classList.toggle('ptt-active', pttSending);
    const problems = [...(capabilities.issues || []), capabilities.error].filter(Boolean).join(' ');
    if (problems) setError(problems);
    else if (errorText.dataset.local !== '1') setError('');
  }

  function playChunk(bytes) {
    if (!context || bytes.length < 2) return;
    const samples = new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.length / 2));
    const buffer = context.createBuffer(1, samples.length, WIRE_RATE);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i += 1) channel[i] = samples[i] / 32768;
    const now = context.currentTime;
    if (playAt < now + 0.02) playAt = now + START_DELAY_SECONDS;
    if (playAt - now > MAX_BUFFERED_SECONDS) return;
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(gain);
    source.start(playAt);
    playAt += buffer.duration;
  }

  async function readStream(signal) {
    const response = await fetch(`${base}/audio`, {cache: 'no-store', signal});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    const reader = response.body.getReader();
    let pending = new Uint8Array(0);
    setState('Lyssnar');
    for (;;) {
      const {done, value} = await reader.read();
      if (done) return;
      const merged = new Uint8Array(pending.length + value.length);
      merged.set(pending);
      merged.set(value, pending.length);
      let offset = 0;
      while (merged.length - offset >= 2) {
        const length = (merged[offset] << 8) | merged[offset + 1];
        if (merged.length - offset - 2 < length) break;
        if (length) playChunk(merged.slice(offset + 2, offset + 2 + length));
        offset += 2 + length;
      }
      pending = merged.slice(offset);
    }
  }

  async function listenLoop(signal) {
    while (!signal.aborted) {
      try {
        await readStream(signal);
      } catch (error) {
        if (signal.aborted) return;
        setError(`Ljudströmmen avbröts: ${error.message}`);
        errorText.dataset.local = '1';
      }
      if (signal.aborted) return;
      setState('Återansluter…');
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }

  function connect() {
    if (connected) return;
    context = context || new (window.AudioContext || window.webkitAudioContext)();
    gain = context.createGain();
    gain.gain.value = Number(volume.value);
    gain.connect(context.destination);
    playAt = 0;
    abort = new AbortController();
    connected = true;
    errorText.dataset.local = '0';
    setError('');
    setState('Ansluter…');
    listenLoop(abort.signal);
    render();
  }

  function disconnect() {
    if (!connected) return;
    releasePtt();
    connected = false;
    abort.abort();
    abort = null;
    closeMic();
    if (context) context.close();
    context = null;
    setState('Frånkopplad');
    render();
  }

  function downsample(input, sourceRate) {
    const ratio = sourceRate / WIRE_RATE;
    const length = Math.floor(input.length / ratio);
    const output = new Int16Array(length);
    for (let i = 0; i < length; i += 1) {
      const start = Math.floor(i * ratio);
      const end = Math.min(input.length, Math.max(start + 1, Math.floor((i + 1) * ratio)));
      let sum = 0;
      for (let j = start; j < end; j += 1) sum += input[j];
      const value = Math.max(-1, Math.min(1, sum / (end - start)));
      output[i] = Math.round(value * 32767);
    }
    return output;
  }

  function post(flag, pcm) {
    const body = new Uint8Array(1 + (pcm ? pcm.byteLength : 0));
    body[0] = flag;
    if (pcm) body.set(new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength), 1);
    if (pendingSends > 6 && flag) return;
    pendingSends += 1;
    sendChain = sendChain.then(async () => {
      try {
        const response = await fetch(`${base}/audio-mic`, {
          method: 'POST', body, headers: {'Content-Type': 'application/octet-stream'},
        });
        if (!response.ok && flag) {
          const data = await response.json().catch(() => ({}));
          errorText.dataset.local = '1';
          setError(data.error || `Sändningen avvisades (HTTP ${response.status}).`);
          pttHeld = false;
          pttSending = false;
          render();
        }
      } catch (error) {
        if (flag) {
          errorText.dataset.local = '1';
          setError(`Kunde inte skicka ljud: ${error.message}`);
        }
      } finally {
        pendingSends -= 1;
      }
    });
  }

  async function openMic() {
    if (mic) return mic;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error('Mikrofon kräver HTTPS eller localhost.');
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true},
    });
    const micContext = context || new (window.AudioContext || window.webkitAudioContext)();
    const source = micContext.createMediaStreamSource(stream);
    const processor = micContext.createScriptProcessor(2048, 1, 1);
    const silent = micContext.createGain();
    silent.gain.value = 0;
    processor.onaudioprocess = (event) => {
      if (!pttSending) return;
      post(1, downsample(event.inputBuffer.getChannelData(0), micContext.sampleRate));
    };
    source.connect(processor);
    processor.connect(silent);
    silent.connect(micContext.destination);
    mic = {stream, source, processor, silent};
    return mic;
  }

  function closeMic() {
    if (!mic) return;
    mic.processor.onaudioprocess = null;
    mic.source.disconnect();
    mic.processor.disconnect();
    mic.silent.disconnect();
    mic.stream.getTracks().forEach((track) => track.stop());
    mic = null;
  }

  async function pressPtt() {
    if (pttHeld || pttButton.disabled) return;
    pttHeld = true;
    errorText.dataset.local = '0';
    setError('');
    try {
      if (!context) {
        context = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (context.state === 'suspended') await context.resume();
      await openMic();
    } catch (error) {
      pttHeld = false;
      errorText.dataset.local = '1';
      setError(`Kunde inte använda mikrofonen: ${error.message}`);
      return;
    }
    if (!pttHeld) return;
    pttSending = true;
    setState('Sänder');
    render();
  }

  function releasePtt() {
    const wasSending = pttSending;
    pttHeld = false;
    pttSending = false;
    if (wasSending) {
      post(0, null);
      setState(connected ? 'Lyssnar' : 'Frånkopplad');
    }
    render();
  }

  connectButton.addEventListener('click', () => (connected ? disconnect() : connect()));
  volume.addEventListener('input', () => { if (gain) gain.gain.value = Number(volume.value); });
  pttButton.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    pttButton.setPointerCapture(event.pointerId);
    pressPtt();
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    pttButton.addEventListener(type, releasePtt);
  }
  pttButton.addEventListener('keydown', (event) => {
    if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) {
      event.preventDefault();
      pressPtt();
    }
  });
  pttButton.addEventListener('keyup', (event) => {
    if (event.key === ' ' || event.key === 'Enter') releasePtt();
  });
  pttButton.addEventListener('blur', releasePtt);
  window.addEventListener('blur', releasePtt);
  window.addEventListener('pagehide', () => { releasePtt(); disconnect(); });

  window.SvxAudio = {
    update(udpAudio, online) {
      capabilities = {...capabilities, ...(udpAudio || {})};
      nodeOnline = Boolean(online);
      if (!nodeOnline && connected) setState('Noden är offline');
      render();
    },
  };
})();
