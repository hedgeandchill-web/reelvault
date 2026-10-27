(() => {
  'use strict';
  const form = document.getElementById('downloader-form');
  const urlField = document.getElementById('instagram-url');
  const pasteButton = document.getElementById('paste-btn');
  const getButton = document.getElementById('get-video-btn');
  const feedback = document.getElementById('feedback');
  const preview = document.getElementById('preview-card');
  const saveButton = document.getElementById('save-video-btn');
  const thumb = document.getElementById('preview-thumbnail');
  const title = document.getElementById('preview-title');
  const meta = document.getElementById('preview-meta');
  const tag = document.getElementById('preview-tag');
  const tabs = [...document.querySelectorAll('.tool-tab')];
  const loadingContent = getButton.querySelector('.loading-content');
  const buttonContent = getButton.querySelector('.button-content');
  const saveContent = saveButton.querySelector('.save-label');
  const saveLoading = saveButton.querySelector('.save-loading');
  let currentToken = null;
  let busy = false;

  document.getElementById('year').textContent = new Date().getFullYear();

  function showFeedback(message, type = 'error') {
    feedback.hidden = false;
    feedback.className = `feedback ${type}`;
    feedback.textContent = message;
  }
  function hideFeedback() {
    feedback.hidden = true;
    feedback.textContent = '';
  }
  function setInspecting(state) {
    busy = state;
    getButton.disabled = state;
    pasteButton.disabled = state;
    buttonContent.hidden = state;
    loadingContent.hidden = !state;
    getButton.setAttribute('aria-busy', String(state));
  }
  function setPreparing(state) {
    busy = state;
    saveButton.disabled = state;
    getButton.disabled = state;
    saveContent.hidden = state;
    saveLoading.hidden = !state;
    saveButton.setAttribute('aria-busy', String(state));
  }
  function validateUrl(value) {
    try {
      const url = new URL(value.trim());
      const validHost = ['www.instagram.com', 'instagram.com', 'm.instagram.com'].includes(url.hostname);
      return url.protocol === 'https:' && validHost && /^\/(reel|reels|p|tv)\/[a-zA-Z0-9_-]{5,40}\/?$/.test(url.pathname);
    } catch {
      return false;
    }
  }
  function readableDuration(seconds) {
    if (!seconds) return null;
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${String(secs).padStart(2, '0')}`;
  }
  async function requestJson(path, data) {
    const options = data === undefined
      ? { method: 'GET', cache: 'no-store' }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
    const response = await fetch(path, options);
    let payload;
    try { payload = await response.json(); }
    catch { throw new Error('The server did not respond as expected. Please retry.'); }
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Something went wrong. Please try again.');
    return payload;
  }
  function setPreview(data) {
    currentToken = data.token;
    title.textContent = data.title || 'Instagram video';
    const parts = ['MP4', 'Best available quality'];
    const duration = readableDuration(data.duration);
    if (duration) parts.push(duration);
    meta.textContent = parts.join(' · ');
    tag.textContent = (data.type || 'Video').toUpperCase();
    const oldImage = thumb.querySelector('img');
    if (oldImage) oldImage.remove();
    if (data.thumbnail) {
      try {
        const imgUrl = new URL(data.thumbnail);
        if (imgUrl.protocol === 'https:') {
          const img = document.createElement('img');
          img.src = imgUrl.toString();
          img.alt = 'Instagram video thumbnail';
          img.loading = 'lazy';
          thumb.appendChild(img);
        }
      } catch { /* Keep the gradient placeholder. */ }
    }
    preview.hidden = false;
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    const url = urlField.value.trim();
    hideFeedback();
    preview.hidden = true;
    currentToken = null;
    if (!validateUrl(url)) {
      showFeedback('Please paste a direct public Instagram Reel or video post link (starting with https://www.instagram.com/).');
      urlField.focus();
      return;
    }
    setInspecting(true);
    try {
      const data = await requestJson('/api/inspect', { url });
      setPreview(data);
    } catch (error) {
      showFeedback(error.message || 'Sorry, this video could not be retrieved.');
    } finally {
      setInspecting(false);
    }
  });

  saveButton.addEventListener('click', async () => {
    if (busy || !currentToken) return;
    hideFeedback();
    setPreparing(true);
    try {
      const started = await requestJson('/api/prepare', { token: currentToken });
      if (!/^\/api\/jobs\/[0-9a-f]{32}$/.test(started.status_url || '')) {
        throw new Error('The download could not be queued. Please retry.');
      }
      let download_url = null;
      const deadline = Date.now() + 10 * 60 * 1000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 1800));
        const job = await requestJson(started.status_url);
        if (job.status === 'error') throw new Error(job.detail || 'Video processing failed.');
        if (job.status === 'done') {
          download_url = job.download_url;
          break;
        }
        saveLoading.lastChild.textContent = job.status === 'queued' ? ' In queue…' : ' Processing video…';
      }
      if (!download_url || !/^\/api\/file\/[0-9a-f]{32}$/.test(download_url)) {
        throw new Error('The video is taking too long. Please retry shortly.');
      }
      const link = document.createElement('a');
      link.href = download_url;
      link.download = 'instagram-video.mp4';
      document.body.appendChild(link);
      link.click();
      link.remove();
      showFeedback('Your download has started. Check your browser’s Downloads folder.', 'success');
    } catch (error) {
      showFeedback(error.message || 'The download failed. Please try again.');
    } finally {
      setPreparing(false);
    }
  });

  pasteButton.addEventListener('click', async () => {
    if (busy) return;
    try {
      const text = await navigator.clipboard.readText();
      if (text.trim()) {
        urlField.value = text.trim();
        preview.hidden = true;
        hideFeedback();
        urlField.focus();
      } else {
        showFeedback('Your clipboard is empty. Copy an Instagram video link first.', 'info');
      }
    } catch {
      urlField.focus();
      showFeedback('Your browser blocked clipboard access. Tap the input and paste the link manually.', 'info');
    }
  });

  urlField.addEventListener('input', () => {
    hideFeedback();
    preview.hidden = true;
    currentToken = null;
  });
  const placeholders = {
    video: 'https://www.instagram.com/reel/…',
    reels: 'https://www.instagram.com/reel/…',
    posts: 'https://www.instagram.com/p/…',
  };
  tabs.forEach(tab => tab.addEventListener('click', () => {
    tabs.forEach(t => {
      const active = t === tab;
      t.classList.toggle('active', active);
      t.setAttribute('aria-pressed', String(active));
    });
    urlField.placeholder = placeholders[tab.dataset.tab] || placeholders.video;
    hideFeedback();
  }));
})();
