# GSP API Playbook

This is the fixed, verified path for checking Dahua GSP PLA status from the Windows agent.

## Do Not Relearn This

Do not start with Playwright/browser DOM automation for GSP status checks. GSP can detect DevTools-controlled browser sessions and redirect them to `about:blank`.

Use `gsp_query_mode: "api"` in `config.json`.

## Authentication

Endpoint:

```text
POST /dahua-b-usercenter/oauth/token
```

Params:

```json
{
  "grant_type": "password",
  "username": "<GSP username>",
  "password": "<GSP password>",
  "client_id": "client_1",
  "client_secret": "B0123456!AbC"
}
```

Successful response includes `access_token` and `refresh_token`. Store it only in ignored local file `gsp_auth.local.json`.

Request headers for GSP business APIs:

```text
Authorization: Bearer <access_token>
Content-Type: application/json;charset=UTF-8
X-Requested-With: XMLHttpRequest
apm-user-no: <GSP username>
```

## PLA List Status

Endpoint:

```text
POST /dahua-b-pricing/priceListApplication/pageByEntity
```

Payload:

```json
{
  "pageNum": 1,
  "pageSize": 10,
  "countryCode": "FR",
  "priceListApplicationId": "PLA20260605143508433"
}
```

Verified result:

```text
PLA20260605143508433 -> Approved
```

Important: the working filter field is `priceListApplicationId`. Fields like `applicationNo` and `plaNo` are ignored or invalid for this endpoint.

## PLA Detail And Approval Owner

Endpoint:

```text
POST /dahua-b-pricing/priceListApplication/getApplicationDetailAndCategory
```

Payload:

```json
{
  "priceListApplicationId": "PLA20260602141029254"
}
```

The response contains:

```text
data.priceListApplication.status
data.priceListApplication.currentStep
data.priceListApplication.taskers
```

Verified Under Approval example:

```text
PLA20260602141029254
status: Under Approval
currentStep: Country Product Manager
taskers: LEON HOU(30195)
```

## Local Commands

Query one PLA directly:

```powershell
.\.venv\Scripts\python.exe .\gsp_status_agent.py --config .\config.json --pla PLA20260602141029254
```

Queue dry run:

```powershell
.\.venv\Scripts\python.exe .\gsp_status_agent.py --config .\config.json --once --no-push --max-tasks 5
```

Production queue run:

```powershell
.\run_once.ps1
```
