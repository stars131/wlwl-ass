import http from 'node:http';

const port = Number(process.env.PORT || 19091);

const server = http.createServer((req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', mode: 'placeholder' }));
    return;
  }
  res.writeHead(501, { 'content-type': 'application/json' });
  res.end(JSON.stringify({
    error: 'not_implemented',
    detail: 'Install and wire UI-TARS SDK packages here when needed.',
  }));
});

server.listen(port, '127.0.0.1', () => {
  console.log(`UI-TARS sidecar placeholder listening on http://127.0.0.1:${port}`);
});
