// HEIMDAL – Azure API Management as LLM + agent gateway (Bifrost).
// Scope: resource group in Sweden Central. No private networking (demo).
targetScope = 'resourceGroup'

@description('Azure region. Sweden Central for model availability.')
param location string = 'swedencentral'

@description('Project prefix used in resource names.')
param prefix string = 'heimdal'

@description('Short unique suffix to keep global names unique (storage, APIM, Foundry subdomain).')
@minLength(3)
@maxLength(8)
param suffix string = take(uniqueString(resourceGroup().id), 6)

@description('Publisher e-mail for APIM.')
param publisherEmail string

@description('Publisher name for APIM.')
param publisherName string = 'Asgard Gateway Ops'

@description('APIM SKU. StandardV2 supports LLM policies and MCP servers.')
@allowed(['BasicV2', 'StandardV2', 'Developer', 'Premium'])
param apimSku string = 'StandardV2'

@description('Deploy MAI-Thinking-1 (preview). Needs GlobalStandard quota, which defaults to 0 – request it first, then set true.')
param enableMai bool = false

@description('Model deployments in Foundry. Each: { name, model, version, format, sku, capacity }.')
param models array = [
  { name: 'gpt-6-luna', model: 'gpt-6-luna', version: '2026-09-22', format: 'OpenAI', sku: 'GlobalStandard', capacity: 100 }
  { name: 'gpt-5-6-terra', model: 'gpt-5.6-terra', version: '2026-07-09', format: 'OpenAI', sku: 'GlobalStandard', capacity: 50 }
  { name: 'gpt-6-1-sol', model: 'gpt-6.1-sol', version: '2026-09-29', format: 'OpenAI', sku: 'GlobalStandard', capacity: 30 }
  { name: 'mai-thinking-1', model: 'MAI-Thinking-1', version: '2026-06-01', format: 'Microsoft', sku: 'GlobalStandard', capacity: 20 }
]

@description('Router tier -> deployment name. pym = small/short, stark = mid, thanos = large/reasoning.')
param tierModels object = {
  pym: 'gpt-6-luna' // luna: small & quick
  stark: 'gpt-5-6-terra' // terra: mid-tier all-rounder
  thanos: 'gpt-6-1-sol' // sol: top tier // replaced by mai-thinking-1 when enableMai = true
}

@description('Deployment used for the LLM-based complexity classifier.')
param classifierModel string = 'gpt-6-luna'

@description('Router settings (changeable at runtime via APIM named values).')
param router object = {
  mode: 'hybrid' // length | complexity | hybrid
  complexityMode: 'heuristic' // heuristic | llm
  lengthThresholdTokens: '500'
  complexityThreshold: '3'
}

@description('Tokens-per-minute limits per product.')
param tokenLimits object = {
  standard: '5000'
  premium: '100000'
}

@description('Demo users: one APIM subscription each.')
param demoUsers array = [
  { name: 'thor', product: 'avengers-initiative' }
  { name: 'loki', product: 'avengers-initiative' }
  { name: 'hulk', product: 'avengers-initiative' }
  { name: 'black-widow', product: 'avengers-initiative' }
  { name: 'tony-stark', product: 'infinity-gauntlet' }
]

var activeModels = enableMai ? models : filter(models, m => m.format != 'Microsoft')
var activeTierModels = enableMai ? union(tierModels, { thanos: 'mai-thinking-1' }) : tierModels

var tags = {
  project: 'heimdal'
  theme: 'mcu'
  purpose: 'apim-llm-gateway-demo'
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    logName: 'log-${prefix}-allseeing-${suffix}'
    appInsightsName: 'appi-${prefix}-allseeing-${suffix}'
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
    accountName: 'fdry-${prefix}-asgard-${suffix}'
    projectName: 'proj-${prefix}-avengers-v2'
    models: activeModels
    workspaceId: monitoring.outputs.workspaceId
    tags: tags
  }
}

module apim 'modules/apim.bicep' = {
  name: 'apim'
  params: {
    location: location
    apimName: 'apim-${prefix}-bifrost-${suffix}'
    sku: apimSku
    publisherEmail: publisherEmail
    publisherName: publisherName
    appInsightsId: monitoring.outputs.appInsightsId
    appInsightsKey: monitoring.outputs.appInsightsKey
    workspaceId: monitoring.outputs.workspaceId
    foundryEndpoint: foundry.outputs.endpoint
    maiDeployments: enableMai ? join(map(filter(activeModels, m => m.format == 'Microsoft'), m => m.name), ',') : 'none' // named values cannot be empty
    tierModels: activeTierModels
    classifierModel: classifierModel
    router: router
    tokenLimits: tokenLimits
    tags: tags
  }
}

module rbac 'modules/rbac.bicep' = {
  name: 'rbac'
  params: {
    foundryAccountName: foundry.outputs.accountName
    apimPrincipalId: apim.outputs.principalId
  }
}

module mcp 'modules/mcp-function.bicep' = {
  name: 'shield-mcp'
  params: {
    location: location
    functionAppName: 'func-${prefix}-shield-mcp-${suffix}'
    planName: 'plan-${prefix}-shield-${suffix}'
    storageName: toLower('st${prefix}${uniqueString(resourceGroup().id)}')
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
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

output resourceGroup string = resourceGroup().name
output apimName string = apim.outputs.name
output gatewayUrl string = apim.outputs.gatewayUrl
output foundryAccountName string = foundry.outputs.accountName
output foundryProjectName string = foundry.outputs.projectName
output foundryEndpoint string = foundry.outputs.endpoint
output functionAppName string = mcp.outputs.name
output appInsightsName string = monitoring.outputs.appInsightsName
output logAnalyticsWorkspaceId string = monitoring.outputs.workspaceCustomerId
output tokenUsageWorkbookId string = workbook.outputs.workbookId
