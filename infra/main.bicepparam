using 'main.bicep'

// Deployment-specific values only. Models, tiers, router defaults, token limits and demo users live in
// infra/config/heimdal.json (shared with azd, scripts/deploy.py and the notebooks).

param location = 'swedencentral'
param publisherEmail = readEnvironmentVariable('HEIMDAL_PUBLISHER_EMAIL', 'heimdal@example.com')
param apimSku = 'StandardV2'

// MAI-Thinking-1 (preview) has 0 default quota: request GlobalStandard quota, then `HEIMDAL_ENABLE_MAI=true`.
param enableMai = toLower(readEnvironmentVariable('HEIMDAL_ENABLE_MAI', 'false')) == 'true'

// Local Ollama via ngrok: `HEIMDAL_ENABLE_OLLAMA=true`; scripts/deploy.py runs scripts/ollama_tunnel.py,
// which starts ollama + ngrok and sets the URL, models and tunnel credentials below.
param enableOllama = toLower(readEnvironmentVariable('HEIMDAL_ENABLE_OLLAMA', 'false')) == 'true'
param ollamaUrl = readEnvironmentVariable('HEIMDAL_OLLAMA_URL', '')
param ollamaModels = readEnvironmentVariable('HEIMDAL_OLLAMA_MODELS', '')
param ollamaAuth = readEnvironmentVariable('HEIMDAL_OLLAMA_AUTH', '')

// Set by scripts/deploy.py: the current Function MCP key (so a redeploy does not reset the named value)
// and the signed-in user (data-plane roles for the notebooks).
param mcpKey = readEnvironmentVariable('HEIMDAL_MCP_KEY', '')
param deployerPrincipalId = readEnvironmentVariable('HEIMDAL_PRINCIPAL_ID', '')
param deployerPrincipalType = readEnvironmentVariable('HEIMDAL_PRINCIPAL_TYPE', 'User')
