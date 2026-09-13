const token = document.querySelector('meta[name="face-sync-token"]').content;
const form = document.getElementById('setup-form');
const message = document.getElementById('message');
const installCard = document.getElementById('install-card');
const steps = document.querySelectorAll('.step');

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-Face-Sync-Token': token, ...(options.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Something went wrong.');
  return body;
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
  document.getElementById('preview-heading').textContent = result.person ? `Preview for ${result.person}` : 'Preview for all faces';
  document.getElementById('stat-correct').textContent = summary.skipped.toLocaleString();
  document.getElementById('stat-assign').textContent = summary.assigned.toLocaleString();
  document.getElementById('stat-create').textContent = summary.inserted.toLocaleString();
  document.getElementById('stat-conflicts').textContent = summary.conflicts.toLocaleString();
  document.getElementById('result-files').textContent = summary.files_digikam.toLocaleString();
  document.getElementById('result-matched').textContent = summary.files_matched.toLocaleString();
  document.getElementById('result-unmatched').textContent = summary.files_unmatched_digikam.toLocaleString();
  document.getElementById('result-warnings').textContent = result.warnings.length.toLocaleString();
  document.getElementById('scope-screen').classList.remove('active');
  document.getElementById('scan-screen').classList.remove('active');
  document.getElementById('preview-screen').classList.add('active');
  steps[1].classList.add('done');
  steps[1].classList.remove('current');
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
}

function updateProgress(status) {
  const data = status.progress;
  const progress = document.getElementById('scan-progress');
  if (data.total > 0) {
    progress.max = data.total;
    progress.value = Math.min(data.current, data.total);
    const percent = Math.floor((100 * data.current) / data.total);
    document.getElementById('progress-percent').textContent = `${percent}%`;
    document.getElementById('progress-count').textContent = `${data.current.toLocaleString()} of ${data.total.toLocaleString()} photos`;
    document.getElementById('scan-phase').textContent = 'Comparing face rectangles and names with Nextcloud…';
  }
  document.getElementById('live-matched').textContent = (data.matched || 0).toLocaleString();
  document.getElementById('live-changes').textContent = ((data.assigned || 0) + (data.inserted || 0)).toLocaleString();
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
  const scope = document.querySelector('input[name="scope"]:checked').value;
  const person = document.getElementById('person-select').value;
  const scopeMessage = document.getElementById('scope-message');
  event.currentTarget.disabled = true;
  event.currentTarget.textContent = 'Starting…';
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
    event.currentTarget.disabled = false;
    event.currentTarget.textContent = 'Preview changes';
  }
});

document.getElementById('preview-back-button').addEventListener('click', () => {
  document.getElementById('preview-screen').classList.remove('active');
  document.getElementById('scope-screen').classList.add('active');
  steps[2].classList.remove('current');
  steps[1].classList.remove('done');
  steps[1].classList.add('current');
});

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
