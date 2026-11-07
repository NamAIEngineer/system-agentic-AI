const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('searchApi', { query: (text) => ipcRenderer.invoke('search:query', text) });
