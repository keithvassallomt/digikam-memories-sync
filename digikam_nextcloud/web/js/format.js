// Number and time formatting. Everything here is presentation only.

export function count(value) {
  return Number(value || 0).toLocaleString();
}

export function plural(value, one, many) {
  return `${count(value)} ${Number(value) === 1 ? one : many}`;
}

function parse(value) {
  if (!value) return null;
  // SQLite timestamps have no zone marker but are written in UTC.
  const text = /\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(value)
    ? `${value.replace(' ', 'T')}Z`
    : value;
  const moment = new Date(text);
  return Number.isNaN(moment.getTime()) ? null : moment;
}

export function timeOfDay(value) {
  const moment = parse(value);
  if (!moment) return '';
  return moment.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** "today 09:12", "yesterday 22:40", or a date for anything older. */
export function when(value) {
  const moment = parse(value);
  if (!moment) return 'never';
  const day = dayLabel(value);
  return day ? `${day} ${timeOfDay(value)}` : moment.toLocaleString();
}

export function dayLabel(value) {
  const moment = parse(value);
  if (!moment) return '';
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const days = Math.floor((startOfToday - moment) / 86400000);
  if (days < 0) return 'today';
  if (days === 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 7) return moment.toLocaleDateString([], { weekday: 'long' });
  return moment.toLocaleDateString();
}

/** "3 days ago", "in 4 minutes". Used for coarse facts, never for precision. */
export function ago(value) {
  const moment = parse(value);
  if (!moment) return 'never';
  const seconds = Math.round((Date.now() - moment.getTime()) / 1000);
  const abs = Math.abs(seconds);
  const units = [
    [60, 'second', 1],
    [3600, 'minute', 60],
    [86400, 'hour', 3600],
    [2592000, 'day', 86400],
  ];
  for (const [limit, name, divisor] of units) {
    if (abs < limit) {
      const amount = Math.max(1, Math.round(abs / divisor));
      const label = `${amount} ${amount === 1 ? name : `${name}s`}`;
      return seconds >= 0 ? `${label} ago` : `in ${label}`;
    }
  }
  return moment.toLocaleDateString();
}

export function percent(current, total) {
  if (!total) return null;
  return Math.max(0, Math.min(100, Math.round((100 * current) / total)));
}
