// Products, demo-user subscriptions, the LLM API (Bifrost router) and the S.H.I.E.L.D. MCP server.
param apimName string
param demoUsers array
param mcpBackendUrl string

resource apim 'Microsoft.ApiManagement/service@2025-09-01-preview' existing = {
  name: apimName
}

// ---- Products -----------------------------------------------------------
resource avengers 'Microsoft.ApiManagement/service/products@2025-09-01-preview' = {
  parent: apim
  name: 'avengers-initiative'
  properties: {
    displayName: 'Avengers Initiative'
    description: 'Standard tier: low token-per-minute limit.'
    subscriptionRequired: true
    approvalRequired: false
    state: 'published'
  }
}

resource gauntlet 'Microsoft.ApiManagement/service/products@2025-09-01-preview' = {
  parent: apim
  name: 'infinity-gauntlet'
  properties: {
    displayName: 'Infinity Gauntlet'
    description: 'Premium tier: high token-per-minute limit.'
    subscriptionRequired: true
    approvalRequired: false
    state: 'published'
  }
}

// ---- LLM API: OpenAI-compatible chat completions / responses (routed) + embeddings ---------------
resource llmApi 'Microsoft.ApiManagement/service/apis@2025-09-01-preview' = {
  parent: apim
  name: 'bifrost-llm'
  properties: {
    displayName: 'Bifrost LLM Gateway'
    description: 'OpenAI-compatible endpoint with per-user token metrics, limits and model routing.'
    path: 'bifrost/v1'
    protocols: ['https']
    subscriptionRequired: true
    subscriptionKeyParameterNames: {
      header: 'api-key'
      query: 'subscription-key' // required by the schema; the policy rejects keys sent in the URL
    }
    serviceUrl: 'https://placeholder.invalid' // overridden by set-backend-service in policy
  }
}

resource chatOp 'Microsoft.ApiManagement/service/apis/operations@2025-09-01-preview' = {
  parent: llmApi
  name: 'chat-completions'
  properties: {
    displayName: 'Chat completions'
    method: 'POST'
    urlTemplate: '/chat/completions'
  }
}

// Responses API (agents, newer SDKs) goes through the same router as chat completions.
resource responsesOp 'Microsoft.ApiManagement/service/apis/operations@2025-09-01-preview' = {
  parent: llmApi
  name: 'responses'
  properties: {
    displayName: 'Responses'
    method: 'POST'
    urlTemplate: '/responses'
  }
}

resource embeddingsOp 'Microsoft.ApiManagement/service/apis/operations@2025-09-01-preview' = {
  parent: llmApi
  name: 'embeddings'
  properties: {
    displayName: 'Embeddings'
    method: 'POST'
    urlTemplate: '/embeddings'
  }
}

resource llmPolicy 'Microsoft.ApiManagement/service/apis/policies@2025-09-01-preview' = {
  parent: llmApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../../policies/bifrost-router.xml')
  }
  dependsOn: [chatOp, responsesOp]
}

// Operation policy without <base />: embeddings skip the chat router (no tiers, llm-* policies don't apply).
resource embeddingsPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2025-09-01-preview' = {
  parent: embeddingsOp
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../../policies/bifrost-embeddings.xml')
  }
}

resource llmGauntletLink 'Microsoft.ApiManagement/service/products/apis@2025-09-01-preview' = {
  parent: gauntlet
  name: llmApi.name
}

resource llmAvengersLink 'Microsoft.ApiManagement/service/products/apis@2025-09-01-preview' = {
  parent: avengers
  name: llmApi.name
}

// ---- MCP server (streamable HTTP passthrough to the Function app) ---------------
// A plain HTTP API, not `type: 'mcp'`: the preview MCP API type ran as a managed-tools server
// (0 tools -> 500 on tools/call) and ignored transportType on this instance. The Function's
// MCP extension already speaks streamable HTTP, so APIM only forwards /mcp unbuffered.
resource mcpApi 'Microsoft.ApiManagement/service/apis@2025-09-01-preview' = {
  parent: apim
  name: 'shield-mcp'
  properties: {
    displayName: 'S.H.I.E.L.D. MCP'
    description: 'Avengers roster, missions and infinity stones as MCP tools (Azure Function).'
    path: 'shield-mcp'
    protocols: ['https']
    subscriptionRequired: true
    subscriptionKeyParameterNames: {
      header: 'api-key'
      query: 'subscription-key' // rejected by the policy
    }
    serviceUrl: mcpBackendUrl
  }
}

var mcpMethods = ['POST', 'GET', 'DELETE']

resource mcpOps 'Microsoft.ApiManagement/service/apis/operations@2025-09-01-preview' = [for m in mcpMethods: {
  parent: mcpApi
  name: 'mcp-${toLower(m)}'
  properties: {
    displayName: 'MCP ${m}'
    method: m
    urlTemplate: '/mcp'
  }
}]

resource mcpPolicy 'Microsoft.ApiManagement/service/apis/policies@2025-09-01-preview' = {
  parent: mcpApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../../policies/shield-mcp.xml')
  }
  dependsOn: [mcpOps]
}

resource mcpGauntletLink 'Microsoft.ApiManagement/service/products/apis@2025-09-01-preview' = {
  parent: gauntlet
  name: mcpApi.name
}

resource mcpAvengersLink 'Microsoft.ApiManagement/service/products/apis@2025-09-01-preview' = {
  parent: avengers
  name: mcpApi.name
}

// ---- Demo users: one subscription per user (identity for token accounting) --------
resource subs 'Microsoft.ApiManagement/service/subscriptions@2025-09-01-preview' = [for u in demoUsers: {
  parent: apim
  name: u.name
  properties: {
    displayName: u.name
    scope: '${apim.id}/products/${u.product}'
    state: 'active'
  }
  dependsOn: [avengers, gauntlet]
}]

output llmPath string = llmApi.properties.path
output mcpPath string = mcpApi.properties.path
