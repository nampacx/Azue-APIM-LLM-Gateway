// Microsoft Foundry (AIServices) account + project + model deployments ("Asgard").
param location string
param accountName string
param projectName string
param workspaceId string
param tags object

@description('Each item: { name, model, version, format, sku, capacity }')
param models array

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  tags: tags
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true // gateway uses managed identity only
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Avengers Initiative'
    description: 'HEIMDAL demo project'
  }
}

// Deployments must be created serially on the same account.
@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [for m in models: {
  parent: account
  name: m.name
  sku: {
    name: m.sku
    capacity: m.capacity
  }
  properties: {
    model: {
      format: m.format
      name: m.model
      version: m.version
    }
  }
}]

resource diag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: account
  properties: {
    workspaceId: workspaceId
    logs: [
      { categoryGroup: 'allLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

output accountName string = account.name
output accountId string = account.id
output projectName string = project.name
output endpoint string = 'https://${account.name}.cognitiveservices.azure.com'
output openAiBase string = 'https://${account.name}.openai.azure.com/openai/v1'
