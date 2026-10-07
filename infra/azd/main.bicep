// azd entry point (subscription scope): creates the Asgard resource group and deploys ../main.bicep into it.
targetScope = 'subscription'

@minLength(1)
@maxLength(32)
@description('azd environment name, e.g. "heimdal-dev".')
param environmentName string

@description('Azure region (also the subscription-level deployment location). Use swedencentral for model availability.')
param location string

@description('Publisher e-mail for APIM.')
param publisherEmail string

@description('"true" to deploy MAI-Thinking-1 (needs GlobalStandard quota).')
param enableMai string = 'false'

@secure()
@description('Current Function mcp_extension key (azd env HEIMDAL_MCP_KEY, stored by the postdeploy hook). Empty on first run.')
param mcpKey string = ''

@description('Signed-in azd user (AZURE_PRINCIPAL_ID): gets Foundry + Log Analytics data roles for the notebooks.')
param principalId string = ''

@description('Type of principalId (AZURE_PRINCIPAL_TYPE).')
param principalType string = 'User'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-heimdal-asgard-${environmentName}'
  location: location
  tags: {
    project: 'heimdal'
    'azd-env-name': environmentName
  }
}

module heimdal '../main.bicep' = {
  name: 'heimdal'
  scope: rg
  params: {
    location: location
    publisherEmail: publisherEmail
    enableMai: toLower(enableMai) == 'true'
    mcpKey: mcpKey
    deployerPrincipalId: principalId
    deployerPrincipalType: principalType
  }
}

// azd turns outputs into environment variables (used by scripts/azd_hook.py).
output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output HEIMDAL_APIM_NAME string = heimdal.outputs.apimName
output HEIMDAL_GATEWAY_URL string = heimdal.outputs.gatewayUrl
output HEIMDAL_FOUNDRY_ACCOUNT string = heimdal.outputs.foundryAccountName
output HEIMDAL_FOUNDRY_PROJECT string = heimdal.outputs.foundryProjectName
output HEIMDAL_FOUNDRY_CONNECTION string = heimdal.outputs.foundryConnectionName
output HEIMDAL_FUNCTION_APP string = heimdal.outputs.functionAppName
output HEIMDAL_APPINSIGHTS string = heimdal.outputs.appInsightsName
output HEIMDAL_LOG_WORKSPACE_ID string = heimdal.outputs.logAnalyticsWorkspaceId
output HEIMDAL_WORKBOOK_ID string = heimdal.outputs.tokenUsageWorkbookId
