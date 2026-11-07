window.searchApi.query('fixture').then(renderResults);
function renderResults(results) { document.body.dataset.resultCount = String(results.length); }
