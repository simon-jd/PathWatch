//go:build pathwatch_lab_bad500

package queryapi

// Canary-drill build only: a deliberately broken release whose query routes
// fail with 500 after the real core read. Never shipped outside deploy/kind.
const labBad500 = true
