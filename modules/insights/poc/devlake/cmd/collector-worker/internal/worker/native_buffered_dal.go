package worker

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"io"

	"github.com/apache/devlake/core/dal"
	coreerrors "github.com/apache/devlake/core/errors"
)

const maxNativeCursorRows = 2000
const maxNativeCursorBytes = 100 * 1024 * 1024

type nativeBufferedDal struct{ dal.Dal }

type nativeSnapshotConnector struct {
	columns []string
	values  [][]driver.Value
}

type nativeSnapshotDriver struct{}
type nativeSnapshotConn struct{ snapshot *nativeSnapshotConnector }
type nativeSnapshotRows struct {
	columns []string
	values  [][]driver.Value
	index   int
}
type nativeSnapshotCursor struct {
	*sql.Rows
	db *sql.DB
}

func (cursor *nativeSnapshotCursor) Close() error {
	err := cursor.Rows.Close()
	if closeErr := cursor.db.Close(); err == nil {
		err = closeErr
	}
	return err
}

func (connector *nativeSnapshotConnector) Connect(context.Context) (driver.Conn, error) {
	return &nativeSnapshotConn{snapshot: connector}, nil
}
func (*nativeSnapshotConnector) Driver() driver.Driver { return nativeSnapshotDriver{} }
func (nativeSnapshotDriver) Open(string) (driver.Conn, error) {
	return nil, errors.New("snapshot requires connector")
}
func (*nativeSnapshotConn) Prepare(string) (driver.Stmt, error) {
	return nil, errors.New("snapshot is read only")
}
func (*nativeSnapshotConn) Close() error { return nil }
func (*nativeSnapshotConn) Begin() (driver.Tx, error) {
	return nil, errors.New("snapshot is read only")
}
func (connection *nativeSnapshotConn) QueryContext(_ context.Context, _ string, _ []driver.NamedValue) (driver.Rows, error) {
	return &nativeSnapshotRows{columns: connection.snapshot.columns, values: connection.snapshot.values}, nil
}
func (rows *nativeSnapshotRows) Columns() []string { return rows.columns }
func (*nativeSnapshotRows) Close() error           { return nil }
func (rows *nativeSnapshotRows) Next(destination []driver.Value) error {
	if rows.index >= len(rows.values) {
		return io.EOF
	}
	copy(destination, rows.values[rows.index])
	rows.index++
	return nil
}

func (buffered *nativeBufferedDal) Cursor(clauses ...dal.Clause) (dal.Rows, coreerrors.Error) {
	original, err := buffered.Dal.Cursor(clauses...)
	if err != nil {
		return nil, err
	}
	columns, columnErr := original.Columns()
	if columnErr != nil {
		original.Close()
		return nil, coreerrors.Convert(columnErr)
	}
	snapshot := &nativeSnapshotConnector{columns: columns}
	total := 0
	for original.Next() {
		if len(snapshot.values) >= maxNativeCursorRows {
			original.Close()
			return nil, coreerrors.Convert(errors.New("native cursor exceeds row limit"))
		}
		values := make([]any, len(columns))
		destinations := make([]any, len(columns))
		for index := range values {
			destinations[index] = &values[index]
		}
		if scanErr := original.Scan(destinations...); scanErr != nil {
			original.Close()
			return nil, coreerrors.Convert(scanErr)
		}
		row := make([]driver.Value, len(columns))
		for index, value := range values {
			if bytes, ok := value.([]byte); ok {
				value = append([]byte(nil), bytes...)
				total += len(bytes)
			}
			if text, ok := value.(string); ok {
				total += len(text)
			}
			row[index] = value
		}
		if total > maxNativeCursorBytes {
			original.Close()
			return nil, coreerrors.Convert(errors.New("native cursor exceeds byte limit"))
		}
		snapshot.values = append(snapshot.values, row)
	}
	originalErr := original.Err()
	closeErr := original.Close()
	if originalErr != nil {
		return nil, coreerrors.Convert(originalErr)
	}
	if closeErr != nil {
		return nil, coreerrors.Convert(closeErr)
	}
	db := sql.OpenDB(snapshot)
	rows, queryErr := db.QueryContext(context.Background(), "snapshot")
	if queryErr != nil {
		db.Close()
		return nil, coreerrors.Convert(queryErr)
	}
	return &nativeSnapshotCursor{Rows: rows, db: db}, nil
}

func (buffered *nativeBufferedDal) Fetch(cursor dal.Rows, destination any) coreerrors.Error {
	if snapshot, ok := cursor.(*nativeSnapshotCursor); ok {
		return buffered.Dal.Fetch(snapshot.Rows, destination)
	}
	return buffered.Dal.Fetch(cursor, destination)
}
