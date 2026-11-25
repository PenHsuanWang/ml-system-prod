// Build-only trust launcher. No corporate input is copied into a runtime image.
package main

import (
	"bytes"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const maxCABytes = 1024 * 1024

func validateCA(data []byte) error {
	count := 0
	for len(bytes.TrimSpace(data)) > 0 {
		data = bytes.TrimSpace(data)
		if !bytes.HasPrefix(data, []byte("-----BEGIN CERTIFICATE-----")) {
			return errors.New("invalid additional CA")
		}
		marker := []byte("-----END CERTIFICATE-----")
		end := bytes.Index(data, marker)
		if end < 0 {
			return errors.New("invalid additional CA")
		}
		end += len(marker)
		block, extra := pem.Decode(data[:end])
		if block == nil || block.Type != "CERTIFICATE" || len(block.Headers) != 0 || len(bytes.TrimSpace(extra)) != 0 {
			return errors.New("invalid additional CA")
		}
		rest := data[end:]
		cert, err := x509.ParseCertificate(block.Bytes)
		now := time.Now()
		if err != nil || !cert.IsCA || !cert.BasicConstraintsValid ||
			(cert.KeyUsage != 0 && cert.KeyUsage&x509.KeyUsageCertSign == 0) || now.Before(cert.NotBefore) || now.After(cert.NotAfter) {
			return errors.New("invalid additional CA")
		}
		count++
		data = rest
	}
	if count == 0 {
		return errors.New("empty additional CA")
	}
	return nil
}

func publicRoots() ([]byte, error) {
	for _, path := range []string{"/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem"} {
		data, err := os.ReadFile(path)
		if err == nil && len(data) > 0 {
			return data, nil
		}
	}
	return nil, errors.New("approved base image with public CA roots required")
}

func execute(args []string, secret string) (code int) {
	if len(args) == 0 {
		fmt.Fprintln(os.Stderr, "build trust: command required")
		return 2
	}
	environment := os.Environ()
	info, err := os.Lstat(secret)
	if err != nil && !os.IsNotExist(err) {
		fmt.Fprintln(os.Stderr, "build trust: CA input unavailable")
		return 2
	}
	if err == nil {
		if !info.Mode().IsRegular() || info.Size() > maxCABytes {
			fmt.Fprintln(os.Stderr, "build trust: invalid CA input")
			return 2
		}
		stream, err := os.Open(secret)
		if err != nil {
			fmt.Fprintln(os.Stderr, "build trust: CA input unavailable")
			return 2
		}
		data, err := io.ReadAll(io.LimitReader(stream, maxCABytes+1))
		stream.Close()
		if err != nil || len(data) > maxCABytes || validateCA(data) != nil {
			fmt.Fprintln(os.Stderr, "build trust: invalid CA input")
			return 2
		}
		roots, err := publicRoots()
		if err != nil {
			fmt.Fprintln(os.Stderr, "build trust: approved public-root base required")
			return 2
		}
		directory, err := os.MkdirTemp("", "ml-build-trust-")
		if err != nil {
			fmt.Fprintln(os.Stderr, "build trust: private workspace unavailable")
			return 2
		}
		defer func() {
			if os.RemoveAll(directory) != nil {
				fmt.Fprintln(os.Stderr, "build trust: temporary CA cleanup failed")
				code = 2
			}
		}()
		bundle := filepath.Join(directory, "ca.pem")
		combined := append(append(roots, '\n'), data...)
		if os.WriteFile(bundle, combined, 0600) != nil {
			fmt.Fprintln(os.Stderr, "build trust: temporary CA unavailable")
			return 2
		}
		overrides := []string{"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PIP_CERT", "GIT_SSL_CAINFO"}
		for _, key := range overrides {
			filtered := environment[:0]
			for _, entry := range environment {
				if !strings.HasPrefix(entry, key+"=") {
					filtered = append(filtered, entry)
				}
			}
			environment = append(filtered, key+"="+bundle)
		}
		if filepath.Base(args[0]) == "apt-get" || filepath.Base(args[0]) == "apt" {
			// Keep APT's unprivileged HTTPS fetch sandbox and a private CA file.
			identity, lookupErr := user.Lookup("_apt")
			if lookupErr != nil {
				fmt.Fprintln(os.Stderr, "build trust: APT sandbox identity unavailable")
				return 2
			}
			uid, uidErr := strconv.Atoi(identity.Uid)
			gid, gidErr := strconv.Atoi(identity.Gid)
			if uidErr != nil || gidErr != nil || os.Chown(directory, uid, gid) != nil || os.Chown(bundle, uid, gid) != nil {
				fmt.Fprintln(os.Stderr, "build trust: APT sandbox CA access unavailable")
				return 2
			}
			args = append([]string{args[0], "-o", "Acquire::https::CaInfo=" + bundle}, args[1:]...)
		}
	}
	command := exec.Command(args[0], args[1:]...)
	command.Env = environment
	command.Stdin, command.Stdout, command.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err := command.Run(); err != nil {
		if failure, ok := err.(*exec.ExitError); ok {
			return failure.ExitCode()
		}
		fmt.Fprintln(os.Stderr, "build trust: command unavailable")
		return 127
	}
	return 0
}

func main() { os.Exit(execute(os.Args[1:], "/run/secrets/corporate_ca")) }
