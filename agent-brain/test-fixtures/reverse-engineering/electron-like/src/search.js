async function search(endpoint, query) {
  const response = await fetch(endpoint + '?q=' + encodeURIComponent(query));
  return response.json();
}
module.exports = { search };
