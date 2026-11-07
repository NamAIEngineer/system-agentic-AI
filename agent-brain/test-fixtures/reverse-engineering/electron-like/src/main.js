const {search} = require('./search');
const config = require('../config.json');
function createWindow() { return { webPreferences: { preload: 'preload.js' } }; }
function handleSearch(query) { return search(config.searchEndpoint, query); }
module.exports = { createWindow, handleSearch };
