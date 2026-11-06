package celldb

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go/aws/credentials"
	"github.com/aws/aws-sdk-go/aws/credentials/stscreds"
	"github.com/jackc/pgx/v5/pgproto3"
)

type renewable struct {
	value   credentials.Value
	failure bool
}

func (p *renewable) Retrieve() (credentials.Value, error) {
	if p.failure {
		return credentials.Value{}, errors.New("secret diagnostic")
	}
	return p.value, nil
}
func (*renewable) IsExpired() bool { return false }

func configForTest(t *testing.T) Config {
	t.Helper()
	server := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer server.Close()
	path := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	return Config{Host: "fixture.cluster-ab.us-east-1.rds.amazonaws.com", Database: "fixture_cell", Username: "fixture_writer", Region: "us-east-1", CAPath: path, MaxOpen: 2}
}

func TestFreshPhysicalConnectionUsesRefreshedWorkloadCredentials(t *testing.T) {
	config := configForTest(t)
	pg, err := connectionConfig(config)
	if err != nil {
		t.Fatal(err)
	}
	if pg.Password != "" || pg.Fallbacks != nil || pg.TLSConfig.InsecureSkipVerify || pg.TLSConfig.ServerName != config.Host || pg.TLSConfig.MinVersion != tls.VersionTLS12 || pg.TLSConfig.RootCAs == nil {
		t.Fatal("unsafe TLS or password defaults")
	}
	provider := &renewable{value: credentials.Value{AccessKeyID: "first-key", SecretAccessKey: "synthetic-secret", SessionToken: "first-session", ProviderName: stscreds.WebIdentityProviderName}}
	credential := credentials.NewCredentials(provider)
	beforeConnect := tokenHook(config, credential)
	if err := beforeConnect(context.Background(), pg); err != nil {
		t.Fatal(err)
	}
	first := pg.Password
	provider.value.AccessKeyID = "rotated-key"
	provider.value.SessionToken = "rotated-session"
	credential.Expire()
	if err := beforeConnect(context.Background(), pg); err != nil {
		t.Fatal(err)
	}
	signed, err := url.Parse("https://" + pg.Password)
	if err != nil {
		t.Fatal(err)
	}
	if first == pg.Password || signed.Query().Get("DBUser") != config.Username || signed.Query().Get("X-Amz-Security-Token") != "rotated-session" || !strings.HasPrefix(signed.Query().Get("X-Amz-Credential"), "rotated-key/") {
		t.Fatal("physical connection did not refresh IAM credentials")
	}
	provider.failure = true
	credential.Expire()
	if err := beforeConnect(context.Background(), pg); err != ErrConnection {
		t.Fatal("refresh failure must be redacted and terminal")
	}
}

func TestRejectsStaticIdentityAndUnsupportedConnectionConfiguration(t *testing.T) {
	config := configForTest(t)
	pg, _ := connectionConfig(config)
	if err := tokenHook(config, credentials.NewStaticCredentials("key", "secret", ""))(context.Background(), pg); err != ErrConnection {
		t.Fatal("static credentials admitted")
	}
	for _, change := range []func(*Config){
		func(c *Config) { c.Host = "localhost" }, func(c *Config) { c.Database = "foreign;drop" },
		func(c *Config) { c.MaxOpen = 0 }, func(c *Config) { c.MaxOpen = 11 }, func(c *Config) { c.CAPath = "missing" },
	} {
		current := config
		change(&current)
		if _, err := connectionConfig(current); err != ErrConfiguration {
			t.Fatal("invalid target admitted")
		}
	}
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "false")
	if _, err := Open(config); err != ErrConfiguration {
		t.Fatal("disabled module opened pool")
	}
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	t.Setenv("AWS_ACCESS_KEY_ID", "static-key")
	if _, err := Open(config); err != ErrConfiguration {
		t.Fatal("ambient credentials admitted")
	}
}

// Exercise pgx's actual physical-connection callback, TLS negotiation and
// PostgreSQL password message. Only the transport destination is substituted.
func TestPGXPhysicalConnectionsSendNewIAMTokensOverVerifiedTLS(t *testing.T) {
	config := configForTest(t)
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(1), DNSNames: []string{config.Host}, NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(config.CAPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	tokens := make(chan string, 2)
	serverErrors := make(chan error, 2)
	go func() {
		for i := 0; i < 2; i++ {
			raw, err := listener.Accept()
			if err != nil {
				serverErrors <- err
				return
			}
			err = func() error {
				defer raw.Close()
				raw.SetDeadline(time.Now().Add(10 * time.Second))
				backend := pgproto3.NewBackend(raw, raw)
				msg, err := backend.ReceiveStartupMessage()
				if err != nil {
					return err
				}
				if _, ok := msg.(*pgproto3.SSLRequest); !ok {
					return fmt.Errorf("expected TLS negotiation")
				}
				if _, err := raw.Write([]byte{'S'}); err != nil {
					return err
				}
				secure := tls.Server(raw, &tls.Config{MinVersion: tls.VersionTLS12, Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}}})
				backend = pgproto3.NewBackend(secure, secure)
				msg, err = backend.ReceiveStartupMessage()
				if err != nil {
					return err
				}
				startup, ok := msg.(*pgproto3.StartupMessage)
				if !ok || startup.Parameters["user"] != config.Username || startup.Parameters["database"] != config.Database {
					return fmt.Errorf("incorrect registered database selection")
				}
				backend.Send(&pgproto3.AuthenticationCleartextPassword{})
				if err := backend.Flush(); err != nil {
					return err
				}
				msg, err = backend.Receive()
				if err != nil {
					return err
				}
				password, ok := msg.(*pgproto3.PasswordMessage)
				if !ok {
					return fmt.Errorf("password message missing")
				}
				tokens <- password.Password
				backend.Send(&pgproto3.AuthenticationOk{})
				backend.Send(&pgproto3.ParameterStatus{Name: "server_version", Value: "16.0"})
				backend.Send(&pgproto3.ParameterStatus{Name: "client_encoding", Value: "UTF8"})
				backend.Send(&pgproto3.BackendKeyData{ProcessID: 1, SecretKey: []byte{0, 0, 0, 1}})
				backend.Send(&pgproto3.ReadyForQuery{TxStatus: 'I'})
				if err := backend.Flush(); err != nil {
					return err
				}
				_, err = backend.Receive()
				return err
			}()
			serverErrors <- err
		}
	}()
	pg, err := connectionConfig(config)
	if err != nil {
		t.Fatal(err)
	}
	pg.DialFunc = func(ctx context.Context, network, address string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, listener.Addr().String())
	}
	pg.LookupFunc = func(context.Context, string) ([]string, error) { return []string{"127.0.0.1"}, nil }
	provider := &renewable{value: credentials.Value{AccessKeyID: "first", SecretAccessKey: "synthetic-secret", SessionToken: "first-session", ProviderName: stscreds.WebIdentityProviderName}}
	credential := credentials.NewCredentials(provider)
	connector := connectorForConfig(config, pg, credential)
	for _, sessionToken := range []string{"first-session", "rotated-session"} {
		provider.value.SessionToken = sessionToken
		credential.Expire()
		conn, err := connector.Connect(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		signed, err := url.Parse("https://" + <-tokens)
		if err != nil || signed.Query().Get("X-Amz-Security-Token") != sessionToken {
			t.Fatal("physical connection used stale token")
		}
		conn.Close()
		if err := <-serverErrors; err != nil {
			t.Fatal(err)
		}
	}
}
