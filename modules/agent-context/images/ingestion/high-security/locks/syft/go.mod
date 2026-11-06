module github.com/anchore/syft

go 1.26.3

retract (
	v1.25.0 // published with a replace directive (confusing for API users)
	v0.53.2
	v0.53.1 // published accidentally with incorrect license in depdencies
)
