# Packaged API documentation assets

The Management API and Prediction Gateway serve the release bundles
below. The files are bundled with the Python package so opening the API reference
requires only the service origin. The adjacent upstream licenses and notices are
included with the package.

| Source | Files | SHA-256 |
| --- | --- | --- |
| [Swagger UI 5.36.0](https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.36.0.tgz) | `swagger-ui-bundle.js` | `b164c5a22535e40dae08c00271674a67e437ec195098c55fcedf8ffac7ac3c88` |
| [Swagger UI 5.36.0](https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.36.0.tgz) | `swagger-ui.css` | `7eb9f5a125e4bc9b3f0b71869b66d1a3d7c501f972f53d44ef91c2ae6c5bda5e` |
| [ReDoc 2.5.3](https://registry.npmjs.org/redoc/-/redoc-2.5.3.tgz) | `redoc.standalone.js` | `488ad6f335c47d69afe969ab3c9a906d5d2b91695d6b3e0be63ab76f63c94021` |

The upstream ReDoc bundle has SHA-256
`1320f442151c57c447d3b70c7ffc6c4f86d08464020fe34c8cc5d3164e9944f0`.
The packaged bundle substitutes a transparent inline image for the remote
`https://cdn.redoc.ly/redoc/logo-mini.svg` image while retaining the attribution
text and link. The API reference can render with outbound requests blocked.

For an upgrade, verify the tarball's npm registry integrity, copy the distribution
files and notices, update the hashes, and run the documentation route checks and a
browser smoke test with requests restricted to the service origin. ReDoc 2.x is
loaded as a classic script by the FastAPI HTML helper.
