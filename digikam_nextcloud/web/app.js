const token = document.querySelector('meta[name="face-sync-token"]').content;
const form = document.getElementById('setup-form');
const message = document.getElementById('message');
const installCard = document.getElementById('install-card');

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
    document.querySelectorAll('.step')[0].classList.add('done');
    document.querySelectorAll('.step')[0].classList.remove('current');
    document.querySelectorAll('.step')[1].classList.add('current');
    document.querySelector('.connection').classList.add('ready');
    document.getElementById('connection-label').textContent = `Connected as ${result.settings.nc_user}`;
  } catch (error) {
    showMessage(error.message, 'error');
  } finally {
    setBusy(false);
  }
});

document.getElementById('back-button').addEventListener('click', () => {
  document.getElementById('scope-screen').classList.remove('active');
  document.getElementById('connect-screen').classList.add('active');
  document.querySelectorAll('.step')[1].classList.remove('current');
  document.querySelectorAll('.step')[0].classList.add('current');
});

async function loadSettings() {
  const settings = await api('/api/settings');
  const fields = ['digikam_library', 'nextcloud_url', 'nc_user', 'nc_photos_path'];
  fields.forEach((name) => {
    if (settings[name]) form.elements[name].value = settings[name];
  });
  if (settings.has_password) document.getElementById('password-help').textContent = 'Saved app password will be used if this is left blank.';
  if (!settings.digikam_library) document.getElementById('discover-button').click();
}

loadSettings().catch((error) => showMessage(error.message, 'error'));
