// Bifrost's outbound lane: a VNet subnet for APIM (v2) outbound VNet integration.
// The gateway itself stays public; the subnet only gives APIM's outbound calls a network identity, so the
// MCP Function can accept traffic from this subnet (service endpoint) and deny everything else.
param location string
param vnetName string
param nsgName string
param tags object

param addressPrefix string = '10.42.0.0/16'
param apimSubnetPrefix string = '10.42.1.0/24' // min /27, /24 recommended for scaling

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: nsgName
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'allow-keyvault-outbound' // APIM dependency (named values, certificates)
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureKeyVault'
          destinationPortRange: '443'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [addressPrefix] }
    subnets: [
      {
        name: 'snet-apim-outbound'
        properties: {
          addressPrefix: apimSubnetPrefix
          networkSecurityGroup: { id: nsg.id }
          delegations: [
            { name: 'apim-v2-integration', properties: { serviceName: 'Microsoft.Web/serverFarms' } }
          ]
          // Calls to *.azurewebsites.net leave via the service endpoint, so the Function sees this subnet.
          serviceEndpoints: [{ service: 'Microsoft.Web' }]
        }
      }
    ]
  }
}

output apimSubnetId string = vnet.properties.subnets[0].id
