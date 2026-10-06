// Azure Monitor workbook: token usage per user and model used by user.
// Reads AppMetrics (llm-emit-token-metric) and ApiManagementGatewayLlmLog from the Log Analytics workspace.
param location string
param workspaceId string
param tags object

var workbookJson = replace(loadTextContent('../workbooks/token-usage.workbook.json'), '__WORKSPACE_ID__', workspaceId)

resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'heimdal-token-usage')
  location: location
  kind: 'shared'
  tags: tags
  properties: {
    displayName: 'Heimdal - LLM token usage'
    category: 'workbook'
    sourceId: workspaceId
    serializedData: workbookJson
  }
}

output workbookId string = workbook.id
