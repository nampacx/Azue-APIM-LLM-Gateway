// HEIMDAL – Azure API Management as LLM + agent gateway (Bifrost).
// Scope: resource group in Sweden Central. Public endpoints; APIM outbound VNet integration locks the MCP Function to the gateway.
targetScope = 'resourceGroup'

@description('Azure region. Sweden Central for model availability.')
param location string = 'swedencentral'

@description('Environment label (dev, test, prod). Tag only; the resource group name carries it.')
param environment string = 'dev'

@description('Project prefix used in resource names (vowels dropped to keep names short).')
param prefix string = 'hmdl'

@description('Short unique suffix to keep global names unique (storage, APIM, Foundry subdomain).')
@minLength(3)
@maxLength(8)
param suffix string = take(uniqueString(resourceGroup().id), 6)

@description('Publisher e-mail for APIM.')
param publisherEmail string

@description('Publisher name for APIM.')
param publisherName string = 'Asgard Gateway Ops'

@description('APIM SKU. A v2 tier is required: LLM policies plus outbound VNet integration (locks the MCP Function to the gateway).')
@allowed(['StandardV2', 'PremiumV2'])
param apimSku string = 'StandardV2'

@description('Deploy MAI-Thinking-1 (preview). Needs GlobalStandard quota, which defaults to 0 – request it first, then set true.')
param enableMai bool = false

@description('Add the local Ollama backend (asgard-ollama) reached through an ngrok tunnel. Prepared by scripts/ollama_tunnel.py.')
param enableOllama bool = false

@description('Public ngrok URL of the local Ollama (no trailing slash). Empty = backend not created.')
param ollamaUrl string = ''

@description('Comma-separated Ollama model names; requests with one of these models go to the local backend.')
param ollamaModels string = ''

@secure()
@description('ngrok basic-auth credentials "user:password" protecting the tunnel; APIM sends them to the backend.')
param ollamaAuth string = ''

@description('Foundry project name.')
param foundryProjectName string = 'proj-${prefix}-vngrs'

@secure()
@description('Function `mcp_extension` system key for the shield-mcp-key named value. Empty on the first deployment; the deploy script / azd hook passes the current key so a redeploy does not reset it.')
param mcpKey string = ''

@description('Object id of the person (or service principal) running the demo. Gets data-plane roles for the notebooks (Foundry agents, Log Analytics queries). Empty = skip.')
param deployerPrincipalId string = ''

@allowed(['User', 'ServicePrincipal', 'Group'])
param deployerPrincipalType string = 'User'

// Defaults below come from infra/config/heimdal.json, shared with scripts/deploy.py and notebooks/heimdal_helpers.py.

@description('Model deployments in Foundry. Each: { name, model, version, format, sku, capacity }. Default: infra/config/heimdal.json.')
param models array = loadJsonContent('config/heimdal.json', '$.models')

@description('Router tier -> deployment name. pym = small/short, stark = mid, thanos = large/reasoning.')
param tierModels object = loadJsonContent('config/heimdal.json', '$.tierModels')

@description('Deployment used for the LLM-based complexity classifier.')
param classifierModel string = loadJsonContent('config/heimdal.json', '$.classifierModel')

@description('Embedding deployment used by POST /embeddings when the client sends model "heimdal-auto".')
param embeddingModel string = loadJsonContent('config/heimdal.json', '$.embeddingModel')

@description('Router settings (changeable at runtime via APIM named values).')
param router object = loadJsonContent('config/heimdal.json', '$.router')

@description('Tokens-per-minute limits per product.')
param tokenLimits object = loadJsonContent('config/heimdal.json', '$.tokenLimits')

@description('Demo users: one APIM subscription each.')
param demoUsers array = loadJsonContent('config/heimdal.json', '$.demoUsers')

@description('Demo user whose subscription key the Foundry project connection (notebook 07) uses.')
param foundryAgentUser string = loadJsonContent('config/heimdal.json', '$.foundryAgentUser')

var activeModels = enableMai ? models : filter(models, m => m.format != 'Microsoft')
var activeTierModels = enableMai ? union(tierModels, { thanos: loadJsonContent('config/heimdal.json', '$.maiThanosModel') }) : tierModels

// Only wire Ollama when the hook delivered a tunnel URL (a backend URL cannot be empty).
var ollamaOn = enableOllama && !empty(ollamaUrl) && !empty(ollamaModels)

var tags = {
  project: 'heimdal'
  environment: environment
  theme: 'mcu'
  purpose: 'apim-llm-gateway-demo'
}

// Outbound VNet integration subnet for APIM; the MCP Function only accepts traffic from it.
module network 'modules/network.bicep' = {
  name: 'network'
  params: {
    location: location
    vnetName: 'vnet-${prefix}-bfrst-${suffix}'
    nsgName: 'nsg-${prefix}-bfrst-${suffix}'
    tags: tags
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    logName: 'log-${prefix}-llsng-${suffix}'
    appInsightsName: 'appi-${prefix}-llsng-${suffix}'
    tags: tags
  }
}

module workbook 'modules/workbook.bicep' = {
  name: 'workbook'
  params: {
    location: location
    workspaceId: monitoring.outputs.workspaceId
    tags: tags
  }
}

module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  params: {
    location: location
    accountName: 'aif-${prefix}-sgrd-${suffix}'
    projectName: foundryProjectName
    models: activeModels
    workspaceId: monitoring.outputs.workspaceId
    tags: tags
  }
}

module apim 'modules/apim.bicep' = {
  name: 'apim'
  params: {
    location: location
    apimName: 'apim-${prefix}-bfrst-${suffix}'
    sku: apimSku
    publisherEmail: publisherEmail
    publisherName: publisherName
    subnetId: network.outputs.apimSubnetId
    appInsightsName: monitoring.outputs.appInsightsName
    workspaceId: monitoring.outputs.workspaceId
    foundryEndpoint: foundry.outputs.endpoint
    maiDeployments: enableMai ? join(map(filter(activeModels, m => m.format == 'Microsoft'), m => m.name), ',') : 'none' // named values cannot be empty
    tierModels: activeTierModels
    classifierModel: classifierModel
    embeddingModel: embeddingModel
    router: router
    tokenLimits: tokenLimits
    mcpKey: mcpKey
    ollamaEnabled: ollamaOn
    ollamaUrl: ollamaUrl
    ollamaModels: ollamaOn ? ollamaModels : 'none' // named values cannot be empty
    ollamaAuth: ollamaAuth
    tags: tags
  }
}

module rbac 'modules/rbac.bicep' = {
  name: 'rbac'
  params: {
    foundryAccountName: foundry.outputs.accountName
    apimPrincipalId: apim.outputs.principalId
    enableMai: enableMai
    workspaceName: monitoring.outputs.workspaceName
    deployerPrincipalId: deployerPrincipalId
    deployerPrincipalType: deployerPrincipalType
  }
}

module mcp 'modules/mcp-function.bicep' = {
  name: 'shield-mcp'
  params: {
    location: location
    functionAppName: 'func-${prefix}-shld-mcp-${suffix}'
    planName: 'asp-${prefix}-shld-${suffix}'
    storageName: take(toLower(replace('st${prefix}${suffix}', '-', '')), 24)
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    allowedSubnetId: network.outputs.apimSubnetId
    tags: tags
  }
}

module apis 'modules/apim-apis.bicep' = {
  name: 'apim-apis'
  params: {
    apimName: apim.outputs.name
    demoUsers: demoUsers
    mcpBackendUrl: 'https://${mcp.outputs.defaultHostName}'
  }
  dependsOn: [rbac]
}

// Foundry project connection to the gateway (bring-your-own-model for Foundry agents, notebook 07).
module foundryConnection 'modules/foundry-connection.bicep' = {
  name: 'foundry-connection'
  params: {
    foundryAccountName: foundry.outputs.accountName
    projectName: foundry.outputs.projectName
    apimName: apim.outputs.name
    llmBaseUrl: '${apim.outputs.gatewayUrl}/${apis.outputs.llmPath}'
    subscriptionName: foundryAgentUser
    extraModels: ollamaOn ? map(split(ollamaModels, ','), m => trim(m)) : [] // local Ollama models for Foundry agents
  }
}

output resourceGroup string = resourceGroup().name
output apimName string = apim.outputs.name
output gatewayUrl string = apim.outputs.gatewayUrl
output foundryAccountName string = foundry.outputs.accountName
output foundryProjectName string = foundry.outputs.projectName
output foundryEndpoint string = foundry.outputs.endpoint
output foundryConnectionName string = foundryConnection.outputs.name
output functionAppName string = mcp.outputs.name
output appInsightsName string = monitoring.outputs.appInsightsName
output logAnalyticsWorkspaceId string = monitoring.outputs.workspaceCustomerId
output tokenUsageWorkbookId string = workbook.outputs.workbookId
output ollamaEnabled bool = ollamaOn
