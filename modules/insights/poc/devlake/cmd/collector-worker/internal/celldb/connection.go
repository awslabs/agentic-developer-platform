// Package celldb supplies the collection/export database pool. Source credentials
// are broker-owned; this package uses only the registered AWS workload identity.
package celldb

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"database/sql"
	"database/sql/driver"
	"errors"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go/aws"
	"github.com/aws/aws-sdk-go/aws/credentials"
	"github.com/aws/aws-sdk-go/aws/credentials/endpointcreds"
	"github.com/aws/aws-sdk-go/aws/credentials/stscreds"
	"github.com/aws/aws-sdk-go/aws/session"
	"github.com/aws/aws-sdk-go/service/rds/rdsutils"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/stdlib"
)

var ErrConfiguration = errors.New("cell_database_configuration_invalid")
var ErrConnection = errors.New("cell_database_connection_unavailable")

var hostPattern = regexp.MustCompile(`^[a-zA-Z0-9.-]+\.rds\.amazonaws\.com$`)
var namePattern = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,62}$`)
var rolePattern = regexp.MustCompile(`^[A-Za-z0-9+=,.@_-]{1,64}$`)
var regionPattern = regexp.MustCompile(`^[a-z]{2}-[a-z]+-[0-9]$`)

type Config struct {
	Host, Database, Username, Region, CAPath string
	MaxOpen                                  int
}

// Open creates a bounded pool, without connecting. Every physical connection
// obtains a newly signed token using the SDK's refreshable workload credentials.
func Open(config Config) (*sql.DB, error) {
	if !strings.EqualFold(os.Getenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED"), "true") ||
		!strings.EqualFold(os.Getenv("DEVLAKE_COLLECTION_ENABLED"), "true") {
		return nil, ErrConfiguration
	}
	for _, variable := range os.Environ() {
		name := strings.SplitN(variable, "=", 2)[0]
		if strings.HasPrefix(name, "PG") || name == "DB_URL" || name == "DATABASE_URL" ||
			name == "AWS_ACCESS_KEY_ID" || name == "AWS_SECRET_ACCESS_KEY" || name == "AWS_SESSION_TOKEN" ||
			name == "AWS_PROFILE" || name == "AWS_SHARED_CREDENTIALS_FILE" || name == "AWS_CONFIG_FILE" {
			return nil, ErrConfiguration
		}
	}
	if (os.Getenv("AWS_ROLE_ARN") == "" || os.Getenv("AWS_WEB_IDENTITY_TOKEN_FILE") == "") &&
		os.Getenv("AWS_CONTAINER_CREDENTIALS_FULL_URI") == "" {
		return nil, ErrConfiguration
	}
	sdk, err := session.NewSessionWithOptions(session.Options{SharedConfigState: session.SharedConfigDisable,
		Config: aws.Config{Region: aws.String(config.Region)}})
	if err != nil {
		return nil, ErrConfiguration
	}
	connector, err := newConnector(config, sdk.Config.Credentials)
	if err != nil {
		return nil, err
	}
	pool := sql.OpenDB(connector)
	pool.SetMaxOpenConns(config.MaxOpen)
	pool.SetMaxIdleConns(config.MaxOpen)
	pool.SetConnMaxIdleTime(5 * time.Minute)
	return pool, nil
}

func connectionConfig(config Config) (*pgx.ConnConfig, error) {
	if !hostPattern.MatchString(config.Host) || !namePattern.MatchString(config.Database) ||
		!rolePattern.MatchString(config.Username) || !regionPattern.MatchString(config.Region) ||
		config.MaxOpen < 1 || config.MaxOpen > 10 {
		return nil, ErrConfiguration
	}
	ca, err := os.ReadFile(config.CAPath)
	if err != nil {
		return nil, ErrConfiguration
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		return nil, ErrConfiguration
	}
	address := url.URL{Scheme: "postgres", Host: config.Host + ":5432", User: url.User(config.Username), Path: "/" + config.Database}
	query := url.Values{"sslmode": {"verify-full"}, "sslrootcert": {config.CAPath}, "connect_timeout": {"10"}}
	address.RawQuery = query.Encode()
	pg, err := pgx.ParseConfig(address.String())
	if err != nil {
		return nil, ErrConfiguration
	}
	pg.Password = ""
	pg.Fallbacks = nil
	pg.TLSConfig = &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots, ServerName: config.Host}
	return pg, nil
}

func tokenHook(config Config, credential *credentials.Credentials) func(context.Context, *pgx.ConnConfig) error {
	return func(ctx context.Context, pg *pgx.ConnConfig) error {
		if credential == nil || ctx.Err() != nil {
			return ErrConnection
		}
		identity, err := credential.GetWithContext(ctx)
		if err != nil || (identity.ProviderName != stscreds.WebIdentityProviderName && identity.ProviderName != endpointcreds.ProviderName) {
			return ErrConnection
		}
		token, err := rdsutils.BuildAuthToken(config.Host+":5432", config.Region, config.Username, credential)
		if err != nil {
			return ErrConnection
		}
		pg.Password = token
		return nil
	}
}

func newConnector(config Config, credential *credentials.Credentials) (driver.Connector, error) {
	pg, err := connectionConfig(config)
	if err != nil {
		return nil, err
	}
	return connectorForConfig(config, pg, credential), nil
}

func connectorForConfig(config Config, pg *pgx.ConnConfig, credential *credentials.Credentials) driver.Connector {
	return redactedConnector{stdlib.GetConnector(*pg, stdlib.OptionBeforeConnect(tokenHook(config, credential)))}
}

// Neither signed tokens nor driver DSNs may escape through a connection error.
type redactedConnector struct{ driver.Connector }

func (connector redactedConnector) Connect(ctx context.Context) (driver.Conn, error) {
	connection, err := connector.Connector.Connect(ctx)
	if err != nil {
		return nil, ErrConnection
	}
	return connection, nil
}
