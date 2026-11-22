export const pages = ['overview', 'inputs', 'run', 'results', 'review', 'delivery', 'quarantine'];

export function route() {
  const [page, id, offset] = location.hash.slice(1).split('/');
  const numeric = Number(id);
  return {page:pages.includes(page) ? page : 'overview',
    id:Number.isFinite(numeric) && id !== '' && numeric > 0 ? numeric : null,
    key:id || null, offset:Math.max(0, Number(offset) || 0)};
}
