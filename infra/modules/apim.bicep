// Bifrost: the API Management gateway instance, observability wiring, backends and router settings.
param location string
param apimName string
param sku string
param publisherEmail string
param publisherName string
param appInsightsId string
@secure()
param appInsightsKey string
param workspaceId string
param foundryEndpoint string
@description('Comma-separated deployment names served by the MAI surface (/mai/v1).')
param maiDeployments string
param tierModels object
param classifierModel string
param router object
param tokenLimits object
param tags object

var apiVersion = '2025-09-01-preview'

resource apim 'Microsoft.ApiManagement/service@2025-09-01-preview' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: sku
    capacity: 1
  }
  identity: { type: 'SystemAssigned' }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

// ---- Observability -------------------------------------------------------
resource logger 'Microsoft.ApiManagement/service/loggers@2025-09-01-preview' = {
  parent: apim
  name: 'heimdal-appinsights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: appInsightsId
    credentials: { instrumentationKey: appInsightsKey }
  }
}

// metrics:true enables custom metrics (llm-emit-token-metric) to App Insights.
resource aiDiag 'Microsoft.ApiManagement/service/diagnostics@2025-09-01-preview' = {
  parent: apim
  name: 'applicationinsights'
  properties: {
    loggerId: logger.id
    alwaysLog: 'allErrors'
    sampling: { samplingType: 'fixed', percentage: 100 }
    metrics: true
    // No body logging configured: response bodies must not be captured globally (breaks MCP streaming).
  }
}

resource azMonLogger 'Microsoft.ApiManagement/service/loggers@2025-09-01-preview' = {
  parent: apim
  name: 'azuremonitor'
  properties: {
    loggerType: 'azureMonitor'
    isBuffered: true
  }
}

resource azMonDiag 'Microsoft.ApiManagement/service/diagnostics@2025-09-01-preview' = {
  parent: apim
  name: 'azuremonitor'
  properties: {
    loggerId: azMonLogger.id
    alwaysLog: 'allErrors'
    sampling: { samplingType: 'fixed', percentage: 100 }
    logClientIp: true
    // Per-request token usage (prompt/completion/total + model) lands in ApiManagementGatewayLlmLog.
    // (requests/responses blocks omitted on purpose: their only option is 'all', which would log prompt contents.)
    largeLanguageModel: {
      logs: 'enabled'
    }
  }
}

resource diagSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: apim
  properties: {
    workspaceId: workspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      { category: 'GatewayLogs', enabled: true }
      { category: 'GatewayLlmLogs', enabled: true }
      { category: 'WebSocketConnectionLogs', enabled: false }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

// ---- Backend: Foundry (OpenAI v1 surface, deployment chosen via "model" in body) ----
resource foundryBackend 'Microsoft.ApiManagement/service/backends@2025-09-01-preview' = {
  parent: apim
  name: 'asgard-foundry'
  properties: {
    protocol: 'http'
    url: '${replace(foundryEndpoint, 'cognitiveservices.azure.com', 'openai.azure.com')}/openai/v1'
    description: 'Foundry models (managed identity)'
  }
}

// MAI models (e.g. MAI-Thinking-1) are served on /mai/v1 of the Foundry resource endpoint.
resource maiBackend 'Microsoft.ApiManagement/service/backends@2025-09-01-preview' = {
  parent: apim
  name: 'asgard-foundry-mai'
  properties: {
    protocol: 'http'
    url: '${replace(foundryEndpoint, 'cognitiveservices.azure.com', 'services.ai.azure.com')}/mai/v1'
    description: 'Foundry MAI models (managed identity)'
  }
}

// ---- Router configuration (runtime-tunable named values) ----------------
var namedValues = [
  { name: 'router-mode', value: router.mode, desc: 'length | complexity | hybrid' }
  { name: 'complexity-mode', value: router.complexityMode, desc: 'heuristic | llm' }
  { name: 'length-threshold-tokens', value: router.lengthThresholdTokens, desc: 'Prompt tokens (est. chars/4) above which the next tier is used' }
  { name: 'complexity-threshold', value: router.complexityThreshold, desc: 'Heuristic score for tier-stark; 2x for tier-thanos' }
  { name: 'model-tier-pym', value: tierModels.pym, desc: 'Deployment for small/simple prompts' }
  { name: 'model-tier-stark', value: tierModels.stark, desc: 'Deployment for medium prompts' }
  { name: 'model-tier-thanos', value: tierModels.thanos, desc: 'Deployment for large/complex/reasoning prompts' }
  { name: 'mai-deployments', value: maiDeployments, desc: 'Deployments served via /mai/v1 (comma-separated)' }
  { name: 'model-classifier', value: classifierModel, desc: 'Deployment used for llm complexity classification' }
  { name: 'foundry-openai-base', value: '${replace(foundryEndpoint, 'cognitiveservices.azure.com', 'openai.azure.com')}/openai/v1', desc: 'Foundry OpenAI v1 base URL' }
  { name: 'tpm-standard', value: tokenLimits.standard, desc: 'Tokens/minute for avengers-initiative' }
  { name: 'tpm-premium', value: tokenLimits.premium, desc: 'Tokens/minute for infinity-gauntlet' }
]

resource nv 'Microsoft.ApiManagement/service/namedValues@2025-09-01-preview' = [for n in namedValues: {
  parent: apim
  name: n.name
  properties: {
    displayName: n.name
    value: n.value
    tags: ['heimdal', 'router']
    secret: false
  }
}]

// Function system key for the MCP backend; real value injected by scripts/deploy.py after code deploy.
resource mcpKey 'Microsoft.ApiManagement/service/namedValues@2025-09-01-preview' = {
  parent: apim
  name: 'shield-mcp-key'
  properties: {
    displayName: 'shield-mcp-key'
    value: 'set-by-deploy-script'
    secret: true
  }
}

output name string = apim.name
output principalId string = apim.identity.principalId
output gatewayUrl string = apim.properties.gatewayUrl
output apiVersionUsed string = apiVersion
