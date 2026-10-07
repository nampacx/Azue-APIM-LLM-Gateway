// Foundry project -> Bifrost connection (bring-your-own-model): Foundry agents can use `<connection>/heimdal-auto`
// as their model, so every agent LLM call goes through the gateway (routing, limits, token metrics).
param foundryAccountName string
param projectName string
param apimName string
@description('Gateway base URL of the LLM API, e.g. https://<apim>.azure-api.net/bifrost/v1')
param llmBaseUrl string
@description('APIM subscription (demo user) the agent runs as – its key is stored in the connection.')
param subscriptionName string
param connectionName string = 'bifrost-apim'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: projectName
}

resource apim 'Microsoft.ApiManagement/service@2025-09-01-preview' existing = {
  name: apimName
}

resource agentSubscription 'Microsoft.ApiManagement/service/subscriptions@2025-09-01-preview' existing = {
  parent: apim
  name: subscriptionName
}

resource connection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: connectionName
  properties: {
    category: 'ApiManagement'
    target: llmBaseUrl
    authType: 'ApiKey'
    credentials: {
      key: agentSubscription.listSecrets().primaryKey
    }
    metadata: {
      deploymentInPath: 'false'
      inferenceAPIVersion: '2024-02-01'
      models: string([
        { name: 'heimdal-auto', properties: { model: { name: 'heimdal-auto', version: '', format: 'OpenAI' } } }
      ])
    }
  }
}

output name string = connection.name
