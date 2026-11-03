// Package schema embeds the idempotent initial ClickHouse migration.
package schema

import _ "embed"

//go:embed 001_init.sql
var Initial string
