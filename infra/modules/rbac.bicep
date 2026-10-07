// Gateway (Bifrost) managed identity may call the models; the demo operator gets the data-plane roles the notebooks need.
param foundryAccountName string
param apimPrincipalId string
param enableMai bool
param workspaceName string

@description('Object id of the demo operator; empty = no operator role assignments.')
param deployerPrincipalId string
param deployerPrincipalType string

// Cognitive Services OpenAI User
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
// Cognitive Services User (needed for non-OpenAI model formats, e.g. Microsoft MAI)
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
// Azure AI User (Foundry projects/agents data plane, notebook 07)
var azureAiUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
// Log Analytics Reader (kql() in the notebooks)
var logAnalyticsReaderRoleId = '73c42c96-874c-492b-b04d-ab87d138a893'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, apimPrincipalId, openAiUserRoleId)
  scope: account
  properties: {
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
  }
}

resource csUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableMai) {
  name: guid(account.id, apimPrincipalId, cognitiveServicesUserRoleId)
  scope: account
  properties: {
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
  }
}

resource deployerAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  name: guid(account.id, deployerPrincipalId, azureAiUserRoleId)
  scope: account
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', azureAiUserRoleId)
  }
}

resource deployerLogReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  name: guid(workspace.id, deployerPrincipalId, logAnalyticsReaderRoleId)
  scope: workspace
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', logAnalyticsReaderRoleId)
  }
}
