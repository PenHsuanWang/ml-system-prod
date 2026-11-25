package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fixtureCA(t *testing.T, isCA bool, expired bool) ([]byte, *ecdsa.PrivateKey) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	end := time.Now().Add(time.Hour)
	if expired {
		end = time.Now().Add(-time.Minute)
	}
	cert := &x509.Certificate{SerialNumber: big.NewInt(42), Subject: pkix.Name{CommonName: "synthetic-build-trust-only"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: end, IsCA: isCA, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}}
	der, err := x509.CreateCertificate(rand.Reader, cert, cert, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), key
}

func TestValidation(t *testing.T) {
	valid, _ := fixtureCA(t, true, false)
	leaf, _ := fixtureCA(t, false, false)
	expired, _ := fixtureCA(t, true, true)
	for _, data := range [][]byte{nil, []byte("invalid-private-input"), leaf, expired, append(valid, []byte("garbage")...), append([]byte("garbage"), valid...), append([]byte("-----BEGIN CERTIFICATE-----\nbad\n-----END CERTIFICATE-----\n"), valid...)} {
		if validateCA(data) == nil {
			t.Fatal("invalid input accepted")
		}
	}
	if validateCA(append(valid, valid...)) != nil {
		t.Fatal("valid CA bundle rejected")
	}
}

func TestCAWithoutOptionalKeyUsage(t *testing.T) {
	valid, key := fixtureCA(t, true, false)
	block, _ := pem.Decode(valid)
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	cert.KeyUsage = 0
	der, err := x509.CreateCertificate(rand.Reader, cert, cert, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	if validateCA(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})) != nil {
		t.Fatal("standards-compatible CA without optional KeyUsage rejected")
	}
}

func TestTLSChild(t *testing.T) {
	if os.Getenv("ML_TRUST_CHILD") != "1" {
		return
	}
	response, err := http.Get(os.Getenv("ML_TRUST_URL"))
	if err != nil {
		os.Exit(7)
	}
	response.Body.Close()
	os.Exit(0)
}

func TestAdditionalTrustPreservesPublicRootsAndCleansUp(t *testing.T) {
	ca, key := fixtureCA(t, true, false)
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	certificate, err := tls.X509KeyPair(ca, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}))
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	server.TLS = &tls.Config{Certificates: []tls.Certificate{certificate}, MinVersion: tls.VersionTLS12}
	server.Config.ErrorLog = log.New(io.Discard, "", 0)
	server.StartTLS()
	defer server.Close()
	directory := t.TempDir()
	secret := filepath.Join(directory, "ca.pem")
	if os.WriteFile(secret, ca, 0600) != nil {
		t.Fatal("secret write failed")
	}
	t.Setenv("ML_TRUST_CHILD", "1")
	t.Setenv("ML_TRUST_URL", server.URL)
	t.Setenv("HTTP_PROXY", "")
	t.Setenv("HTTPS_PROXY", "")
	t.Setenv("ALL_PROXY", "")
	if execute([]string{os.Args[0], "-test.run=^TestTLSChild$"}, filepath.Join(directory, "absent")) != 7 {
		t.Fatal("default trust unexpectedly accepted synthetic root")
	}
	if execute([]string{os.Args[0], "-test.run=^TestTLSChild$"}, secret) != 0 {
		t.Fatal("additional verified trust failed")
	}
	observed := filepath.Join(directory, "observed")
	script := `test "$SSL_CERT_FILE" = "$PIP_CERT" && test "$PIP_CERT" = "$GIT_SSL_CAINFO" && test "$GIT_SSL_CAINFO" = "$REQUESTS_CA_BUNDLE" && cat "$SSL_CERT_FILE" > "$1" && printf '%s' "$SSL_CERT_FILE" > "$2"`
	copied := filepath.Join(directory, "combined")
	if execute([]string{"sh", "-ec", script, "sh", copied, observed}, secret) != 0 {
		t.Fatal("scoped trust variables not propagated")
	}
	combined, _ := os.ReadFile(copied)
	roots, _ := publicRoots()
	if !strings.HasPrefix(string(combined), string(roots)) || !strings.Contains(string(combined), string(ca)) {
		t.Fatal("public roots not preserved")
	}
	path, _ := os.ReadFile(observed)
	if _, err := os.Stat(string(path)); !os.IsNotExist(err) {
		t.Fatal("build CA persisted")
	}
	if execute([]string{"sh", "-c", "exit 9"}, secret) != 9 {
		t.Fatal("child failure was lost")
	}
}

func TestOptionalAndInvalidInputs(t *testing.T) {
	directory := t.TempDir()
	missing := filepath.Join(directory, "absent")
	t.Setenv("PIP_CERT", "original-public-trust")
	if execute([]string{"sh", "-ec", `test "$PIP_CERT" = original-public-trust`}, missing) != 0 {
		t.Fatal("no-secret defaults changed")
	}
	secret := filepath.Join(directory, "invalid")
	for _, data := range [][]byte{[]byte("private-invalid-input"), make([]byte, maxCABytes+1)} {
		os.WriteFile(secret, data, 0600)
		marker := filepath.Join(directory, "must-not-exist")
		if execute([]string{"touch", marker}, secret) != 2 {
			t.Fatal("invalid input did not fail")
		}
		if _, err := os.Stat(marker); !os.IsNotExist(err) {
			t.Fatal("command ran before CA validation")
		}
	}
	link := filepath.Join(directory, "link")
	os.Symlink(secret, link)
	if execute([]string{"true"}, link) != 2 {
		t.Fatal("symlink accepted")
	}
}

func TestAPTUsesVerifiedScopedCA(t *testing.T) {
	directory := t.TempDir()
	ca, _ := fixtureCA(t, true, false)
	secret := filepath.Join(directory, "ca.pem")
	os.WriteFile(secret, ca, 0600)
	binary := filepath.Join(directory, "apt-get")
	os.WriteFile(binary, []byte("#!/bin/sh\ntest \"$1\" = -o && test \"$2\" = Acquire::https::CaInfo=\"$SSL_CERT_FILE\" && test \"$3\" = update\n"), 0700)
	if execute([]string{binary, "update"}, secret) != 0 {
		t.Fatal("APT scoped CA option missing")
	}
}
