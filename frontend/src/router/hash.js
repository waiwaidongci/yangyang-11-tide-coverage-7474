const pages = new Set(['observations','analysis','result','reports','shift']);
export const route = () => {
  const [path, query = ''] = location.hash.slice(1).split('?');
  const [page, id] = path.split('/');
  const params = Object.fromEntries(new URLSearchParams(query));
  return {page:pages.has(page) ? page : 'observations', id, ...params};
};
