# Regional workspace cost evidence

Workspace availability and bounded pricing are separate checks. The offline plan
checker keeps the existing us-east-1 table and selects independent Oregon rates
from `regional-pricing.json` for us-west-2. It refuses an enabled region without a
reviewed service price table, or an instance type missing from that region's table.
No EC2 multiplier is applied to other services.

The Oregon snapshot was collected on 2026-10-12 through AWS Price List
`GetProducts` in us-east-1, filtering products by `regionCode=us-west-2`. Each
quoted product records its service, SKU, usage type, effective date, rate code,
unit and undiscounted OnDemand USD price. EC2 additionally selects Linux, shared
tenancy, no preinstalled software, and used capacity status. EKS extended support
adds the published extended-support surcharge to the standard cluster charge,
consistent with https://aws.amazon.com/eks/pricing/.

The estimate includes its selected region, review date and regional quote evidence.
Usage-driven costs remain explicitly excluded; this is not a total billing cap.
Refresh each service's quotes independently when adding or updating a region.

A runtime deployment must include the JSON catalog with the workspace module.
Adding pricing support does not make an existing interrupted operation retryable:
retain its original saved plan and use an applicable, reviewed recovery procedure.
