TEST-ONLY TLS material for tests/test_net.py (local TLS server + fake CONNECT proxy on 127.0.0.1).

  test_ca.pem      a throw-away CA certificate. Its private key was DELETED right after signing,
                   so no further certificate can ever be issued from it. Only tests load it, explicitly,
                   into a test copy of the client context; the application never does (it verifies
                   against the system trust store only).
  test_server.pem  certificate + private key for "wallex-test.localhost" and 127.0.0.1 only (never for
                   api.wallex.ir), signed by the CA above. Used only by the in-process test server.

Generated 2026-10-23 with OpenSSL 3.1.4 (RSA 2048, SHA-256, valid until 2126, extensions that pass
VERIFY_X509_STRICT: CA basicConstraints/keyUsage/SKI; leaf SAN/EKU serverAuth/AKI/SKI).
Never install test_ca.pem into any trust store.
