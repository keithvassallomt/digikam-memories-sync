const token = document.querySelector('meta[name="face-sync-token"]').content;
const form = document.getElementById('setup-form');
const message = document.getElementById('message');
const installCard = document.getElementById('install-card');
const steps = document.querySelectorAll('.step');
let currentRunId = null;
let currentConflicts = [];
let currentConflictIndex = 0;
let selectedResolution = null;
let conflictPhotoUrl = null;
let conflictPhotoRequest = 0;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-Face-Sync-Token': token, ...(options.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Something went wrong.');
  return body;
}

async function apiBlob(path) {
  const response = await fetch(path, { headers: { 'X-Face-Sync-Token': token } });
  if (!response.ok) {
    let messageText = 'The photo could not be loaded.';
    try {
      const body = await response.json();
      messageText = body.error || messageText;
    } catch (_) {
      // Keep the plain fallback for non-JSON failures.
    }
    throw new Error(messageText);
  }
  return response.blob();
}

function payload() {
  return Object.fromEntries(new FormData(form).entries());
}

function showMessage(text, kind = '') {
  message.textContent = text;
  message.className = `message ${kind}`;
}

function setBusy(busy) {
  document.getElementById('test-button').disabled = busy;
  document.getElementById('save-button').disabled = busy;
}

function showRequirement(result) {
  installCard.hidden = result.face_sync_installed;
  if (result.install_url) document.getElementById('install-link').href = result.install_url;
}

async function testConnection() {
  setBusy(true);
  installCard.hidden = true;
  showMessage('Checking digiKam, Nextcloud and the required apps…');
  try {
    const result = await api('/api/connection/test', { method: 'POST', body: JSON.stringify(payload()) });
    showRequirement(result);
    if (result.ready) showMessage('Everything is connected and ready.', 'success');
    else showMessage('Recognize is ready. Install the Face Sync app to continue.', 'error');
    document.getElementById('digikam-result').textContent = `✓ Found ${result.digikam_db}`;
    return result;
  } catch (error) {
    showMessage(error.message, 'error');
    return null;
  } finally {
    setBusy(false);
  }
}

document.getElementById('test-button').addEventListener('click', testConnection);
document.getElementById('check-again').addEventListener('click', testConnection);
document.getElementById('discover-button').addEventListener('click', async () => {
  try {
    const result = await api('/api/digikam/discover');
    if (result.databases.length) {
      const database = result.databases[0];
      document.getElementById('digikam-library').value = database.replace(/[/\\]digikam4\.db$/, '');
      document.getElementById('digikam-result').textContent = `✓ Found ${database}`;
    } else {
      document.getElementById('digikam-result').textContent = 'No library was found automatically. Enter its folder above.';
    }
  } catch (error) {
    showMessage(error.message, 'error');
  }
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  setBusy(true);
  showMessage('Checking and saving…');
  try {
    const result = await api('/api/settings', { method: 'POST', body: JSON.stringify(payload()) });
    showRequirement(result);
    if (!result.ready) {
      showMessage('Install the Face Sync app before continuing.', 'error');
      return;
    }
    document.getElementById('connect-screen').classList.remove('active');
    document.getElementById('scope-screen').classList.add('active');
    steps[0].classList.add('done');
    steps[0].classList.remove('current');
    steps[1].classList.add('current');
    document.querySelector('.connection').classList.add('ready');
    document.getElementById('connection-label').textContent = `Connected as ${result.settings.nc_user}`;
    await loadPeople();
  } catch (error) {
    showMessage(error.message, 'error');
  } finally {
    setBusy(false);
  }
});

document.getElementById('back-button').addEventListener('click', () => {
  document.getElementById('scope-screen').classList.remove('active');
  document.getElementById('connect-screen').classList.add('active');
  steps[1].classList.remove('current');
  steps[0].classList.add('current');
});

document.querySelectorAll('input[name="scope"]').forEach((radio) => radio.addEventListener('change', () => {
  document.getElementById('person-field').hidden = radio.value === 'all' && radio.checked;
}));

async function loadPeople() {
  const result = await api('/api/people');
  const select = document.getElementById('person-select');
  select.replaceChildren();
  result.people.forEach((person) => {
    const option = document.createElement('option');
    option.value = person;
    option.textContent = person;
    select.appendChild(option);
  });
  const gail = [...select.options].find((option) => option.value === 'Gail Vassallo');
  if (gail) select.value = gail.value;
}

function showPreview(result) {
  const summary = result.summary;
  currentRunId = result.run_id;
  document.getElementById('preview-heading').textContent = result.person ? `Preview for ${result.person}` : 'Preview for all faces';
  document.getElementById('stat-correct').textContent = summary.skipped.toLocaleString();
  document.getElementById('stat-memories').textContent = (summary.assigned + summary.inserted).toLocaleString();
  document.getElementById('stat-digikam').textContent = summary.created_in_digikam.toLocaleString();
  document.getElementById('stat-conflicts').textContent = summary.conflicts.toLocaleString();
  document.getElementById('result-files').textContent = summary.files_digikam.toLocaleString();
  document.getElementById('result-matched').textContent = summary.files_matched.toLocaleString();
  document.getElementById('result-unmatched').textContent = summary.files_unmatched_digikam.toLocaleString();
  document.getElementById('result-memories-files').textContent = summary.files_memories.toLocaleString();
  document.getElementById('result-unmatched-digikam').textContent = summary.files_unmatched_nextcloud.toLocaleString();
  document.getElementById('result-assign').textContent = summary.assigned.toLocaleString();
  document.getElementById('result-create-memories').textContent = summary.inserted.toLocaleString();
  document.getElementById('result-create-digikam').textContent = summary.created_in_digikam.toLocaleString();
  document.getElementById('result-warnings').textContent = result.warnings.length.toLocaleString();
  const reviewButton = document.getElementById('review-conflicts-button');
  reviewButton.disabled = summary.conflicts === 0;
  reviewButton.textContent = summary.conflicts
    ? `Review ${summary.conflicts.toLocaleString()} conflicts`
    : 'No conflicts to review';
  document.getElementById('scope-screen').classList.remove('active');
  document.getElementById('scan-screen').classList.remove('active');
  document.getElementById('preview-screen').classList.add('active');
  steps[1].classList.add('done');
  steps[1].classList.remove('current');
  steps[2].classList.add('current');
}

function conflictCrop(rectangles) {
  const left = Math.min(...rectangles.map((rect) => rect[0]));
  const top = Math.min(...rectangles.map((rect) => rect[1]));
  const right = Math.max(...rectangles.map((rect) => rect[0] + rect[2]));
  const bottom = Math.max(...rectangles.map((rect) => rect[1] + rect[3]));
  const centreX = (left + right) / 2;
  const centreY = (top + bottom) / 2;
  const faceWidth = Math.max(0.01, right - left);
  const faceHeight = Math.max(0.01, bottom - top);
  const padding = Math.max(0.04, Math.max(faceWidth, faceHeight) * 0.7);
  let width = Math.min(1, Math.max(0.18, faceWidth + 2 * padding));
  let height = Math.min(1, Math.max(0.135, faceHeight + 2 * padding));
  const targetAspect = 4 / 3;
  if (width / height < targetAspect) width = Math.min(1, height * targetAspect);
  else height = Math.min(1, width / targetAspect);
  const x = Math.max(0, Math.min(1 - width, centreX - width / 2));
  const y = Math.max(0, Math.min(1 - height, centreY - height / 2));
  return { x, y, width, height };
}

function placeFaceBox(element, rect, crop) {
  element.style.left = `${100 * (rect[0] - crop.x) / crop.width}%`;
  element.style.top = `${100 * (rect[1] - crop.y) / crop.height}%`;
  element.style.width = `${100 * rect[2] / crop.width}%`;
  element.style.height = `${100 * rect[3] / crop.height}%`;
  element.hidden = false;
}

function chooseResolution(resolution) {
  selectedResolution = resolution;
  document.getElementById('keep-digikam-button').setAttribute('aria-pressed', String(resolution === 'digikam'));
  document.getElementById('keep-memories-button').setAttribute('aria-pressed', String(resolution === 'memories'));
  document.getElementById('save-conflict-button').disabled = false;
}

async function loadConflictPhoto(conflict) {
  const requestNumber = ++conflictPhotoRequest;
  const image = document.getElementById('conflict-photo');
  const canvas = document.getElementById('conflict-crop');
  const loading = document.getElementById('photo-loading');
  const digikamBox = document.getElementById('digikam-face-box');
  const memoriesBox = document.getElementById('memories-face-box');
  if (conflictPhotoUrl) URL.revokeObjectURL(conflictPhotoUrl);
  conflictPhotoUrl = null;
  image.hidden = true;
  image.removeAttribute('src');
  canvas.hidden = true;
  digikamBox.hidden = true;
  memoriesBox.hidden = true;
  loading.hidden = false;
  loading.textContent = 'Loading photo…';
  try {
    const blob = await apiBlob(`/api/runs/${currentRunId}/conflicts/${conflict.id}/photo`);
    if (requestNumber !== conflictPhotoRequest) return;
    conflictPhotoUrl = URL.createObjectURL(blob);
    image.onload = () => {
      if (requestNumber !== conflictPhotoRequest) return;
      const crop = conflictCrop([conflict.digikam_rect, conflict.nextcloud_rect]);
      const context = canvas.getContext('2d');
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.drawImage(
        image,
        crop.x * image.naturalWidth,
        crop.y * image.naturalHeight,
        crop.width * image.naturalWidth,
        crop.height * image.naturalHeight,
        0,
        0,
        canvas.width,
        canvas.height,
      );
      loading.hidden = true;
      canvas.hidden = false;
      placeFaceBox(digikamBox, conflict.digikam_rect, crop);
      placeFaceBox(memoriesBox, conflict.nextcloud_rect, crop);
    };
    image.onerror = () => {
      if (requestNumber !== conflictPhotoRequest) return;
      loading.hidden = false;
      loading.textContent = 'This photo format cannot be previewed here.';
    };
    image.src = conflictPhotoUrl;
  } catch (error) {
    if (requestNumber !== conflictPhotoRequest) return;
    loading.hidden = false;
    loading.textContent = error.message;
  }
}

function showConflict(index) {
  currentConflictIndex = index;
  const conflict = currentConflicts[index];
  selectedResolution = null;
  document.getElementById('conflict-review').hidden = false;
  document.getElementById('conflict-complete').hidden = true;
  document.getElementById('conflict-position').textContent = `${index + 1} / ${currentConflicts.length}`;
  const remaining = currentConflicts.filter((item) => item.status !== 'resolved').length;
  document.getElementById('conflict-lead').textContent = `Conflict ${index + 1} of ${currentConflicts.length} · ${remaining} remaining`;
  document.getElementById('conflict-path').textContent = conflict.path;
  document.getElementById('conflict-overlap').textContent = `The two face boxes overlap ${Math.round(100 * conflict.iou)}%.`;
  document.getElementById('digikam-choice-name').textContent = conflict.digikam_person || 'Unnamed';
  document.getElementById('memories-choice-name').textContent = conflict.nextcloud_person || 'Unnamed';
  document.getElementById('digikam-box-label').textContent = `digiKam: ${conflict.digikam_person || 'Unnamed'}`;
  document.getElementById('memories-box-label').textContent = `Memories: ${conflict.nextcloud_person || 'Unnamed'}`;
  document.getElementById('conflict-crop').setAttribute(
    'aria-label',
    `Zoomed face conflict: digiKam says ${conflict.digikam_person || 'Unnamed'}; Memories says ${conflict.nextcloud_person || 'Unnamed'}`,
  );
  document.getElementById('keep-digikam-button').setAttribute('aria-pressed', 'false');
  document.getElementById('keep-memories-button').setAttribute('aria-pressed', 'false');
  document.getElementById('save-conflict-button').disabled = true;
  document.getElementById('apply-all-conflicts').checked = false;
  document.getElementById('conflict-message').textContent = '';
  document.getElementById('conflict-message').className = 'message';
  if (conflict.resolution) chooseResolution(conflict.resolution);
  loadConflictPhoto(conflict);
}

function showConflictComplete() {
  document.getElementById('conflict-review').hidden = true;
  document.getElementById('conflict-complete').hidden = false;
  document.getElementById('conflict-complete-message').textContent = `All ${currentConflicts.length.toLocaleString()} decisions are ready for the Apply step.`;
}

async function openConflictScreen() {
  const result = await api(`/api/runs/${currentRunId}/conflicts`);
  currentConflicts = result.conflicts;
  document.getElementById('preview-screen').classList.remove('active');
  document.getElementById('conflict-screen').classList.add('active');
  steps[2].classList.add('done');
  steps[2].classList.remove('current');
  steps[3].classList.add('current');
  const firstOpen = currentConflicts.findIndex((conflict) => conflict.status !== 'resolved');
  if (firstOpen === -1) showConflictComplete();
  else showConflict(firstOpen);
}

function returnToPreview() {
  conflictPhotoRequest += 1;
  if (conflictPhotoUrl) URL.revokeObjectURL(conflictPhotoUrl);
  conflictPhotoUrl = null;
  document.getElementById('conflict-screen').classList.remove('active');
  document.getElementById('preview-screen').classList.add('active');
  steps[3].classList.remove('current');
  steps[2].classList.remove('done');
  steps[2].classList.add('current');
}

function showScan() {
  document.getElementById('scope-screen').classList.remove('active');
  document.getElementById('scan-screen').classList.add('active');
  steps[1].classList.add('done');
  steps[1].classList.remove('current');
  steps[2].classList.add('current');
  const progress = document.getElementById('scan-progress');
  progress.removeAttribute('value');
  progress.removeAttribute('max');
  document.getElementById('scan-phase').textContent = 'Counting the selected digiKam photos…';
  document.getElementById('progress-percent').textContent = 'Preparing…';
  document.getElementById('progress-count').textContent = 'Counting photos';
  document.getElementById('live-matched').textContent = '0';
  document.getElementById('live-changes').textContent = '0';
  document.getElementById('live-conflicts').textContent = '0';
}

function updateProgress(status) {
  const data = status.progress;
  const progress = document.getElementById('scan-progress');
  if (data.total > 0) {
    progress.max = data.total;
    progress.value = Math.min(data.current, data.total);
    const percent = Math.floor((100 * data.current) / data.total);
    document.getElementById('progress-percent').textContent = `${percent}%`;
    document.getElementById('progress-count').textContent = `${data.current.toLocaleString()} of ${data.total.toLocaleString()} comparisons`;
    document.getElementById('scan-phase').textContent = data.phase === 'scanning_memories'
      ? 'Checking faces from Memories against digiKam…'
      : 'Checking faces from digiKam against Memories…';
  } else if (data.phase === 'loading_memories') {
    progress.removeAttribute('value');
    progress.removeAttribute('max');
    document.getElementById('progress-percent').textContent = 'Loading…';
    document.getElementById('progress-count').textContent = `${(data.loaded_faces || 0).toLocaleString()} Memories faces loaded`;
    document.getElementById('scan-phase').textContent = 'Loading named faces from Memories…';
  }
  document.getElementById('live-matched').textContent = (data.matched || 0).toLocaleString();
  document.getElementById('live-changes').textContent = ((data.assigned || 0) + (data.inserted || 0) + (data.created_in_digikam || 0)).toLocaleString();
  document.getElementById('live-conflicts').textContent = (data.conflicts || 0).toLocaleString();
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForPreview(runId) {
  while (true) {
    const status = await api(`/api/runs/${runId}`);
    updateProgress(status);
    if (status.status === 'previewed') {
      showPreview(status.result);
      return;
    }
    if (status.status === 'failed') throw new Error(status.error || 'The preview failed.');
    await wait(750);
  }
}

document.getElementById('preview-button').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const scope = document.querySelector('input[name="scope"]:checked').value;
  const person = document.getElementById('person-select').value;
  const scopeMessage = document.getElementById('scope-message');
  button.disabled = true;
  button.textContent = 'Starting…';
  scopeMessage.textContent = '';
  scopeMessage.className = 'message';
  try {
    const job = await api('/api/preview', { method: 'POST', body: JSON.stringify({ scope, person }) });
    showScan();
    await waitForPreview(job.run_id);
  } catch (error) {
    document.getElementById('scan-screen').classList.remove('active');
    document.getElementById('scope-screen').classList.add('active');
    steps[2].classList.remove('current');
    steps[1].classList.remove('done');
    steps[1].classList.add('current');
    scopeMessage.textContent = error.message;
    scopeMessage.className = 'message error';
  } finally {
    button.disabled = false;
    button.textContent = 'Preview changes';
  }
});

document.getElementById('preview-back-button').addEventListener('click', () => {
  document.getElementById('preview-screen').classList.remove('active');
  document.getElementById('scope-screen').classList.add('active');
  steps[2].classList.remove('current');
  steps[1].classList.remove('done');
  steps[1].classList.add('current');
});

document.getElementById('review-conflicts-button').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  const originalText = button.textContent;
  button.textContent = 'Opening conflicts…';
  try {
    await openConflictScreen();
  } catch (error) {
    button.textContent = error.message;
  } finally {
    button.disabled = false;
    if (button.textContent === 'Opening conflicts…') button.textContent = originalText;
  }
});

document.getElementById('keep-digikam-button').addEventListener('click', () => chooseResolution('digikam'));
document.getElementById('keep-memories-button').addEventListener('click', () => chooseResolution('memories'));

document.getElementById('save-conflict-button').addEventListener('click', async (event) => {
  if (!selectedResolution) return;
  const button = event.currentTarget;
  const conflict = currentConflicts[currentConflictIndex];
  const applyToRemaining = document.getElementById('apply-all-conflicts').checked;
  button.disabled = true;
  button.textContent = 'Saving…';
  try {
    const result = await api(`/api/runs/${currentRunId}/conflicts/${conflict.id}`, {
      method: 'POST',
      body: JSON.stringify({ resolution: selectedResolution, apply_to_remaining: applyToRemaining }),
    });
    currentConflicts = result.conflicts;
    const laterOpen = currentConflicts.findIndex((item, index) => index > currentConflictIndex && item.status !== 'resolved');
    const nextOpen = laterOpen >= 0
      ? laterOpen
      : currentConflicts.findIndex((item) => item.status !== 'resolved');
    if (nextOpen === -1) showConflictComplete();
    else showConflict(nextOpen);
  } catch (error) {
    const conflictMessage = document.getElementById('conflict-message');
    conflictMessage.textContent = error.message;
    conflictMessage.className = 'message error';
    button.disabled = false;
  } finally {
    button.textContent = 'Save choice and continue';
  }
});

document.getElementById('conflict-back-button').addEventListener('click', () => {
  if (currentConflictIndex > 0) showConflict(currentConflictIndex - 1);
  else returnToPreview();
});

document.getElementById('conflict-complete-back').addEventListener('click', returnToPreview);
document.getElementById('review-decisions-button').addEventListener('click', () => showConflict(0));

async function loadSettings() {
  const settings = await api('/api/settings');
  const fields = ['digikam_library', 'nextcloud_url', 'nc_user', 'nc_photos_path'];
  fields.forEach((name) => {
    if (settings[name]) form.elements[name].value = settings[name];
  });
  if (settings.has_password) document.getElementById('password-help').textContent = 'Saved app password will be used if this is left blank.';
  if (!settings.digikam_library) document.getElementById('discover-button').click();
  else {
    document.querySelector('.connection').classList.add('ready');
    document.getElementById('connection-label').textContent = `Connected as ${settings.nc_user}`;
  }
}

loadSettings().catch((error) => showMessage(error.message, 'error'));
