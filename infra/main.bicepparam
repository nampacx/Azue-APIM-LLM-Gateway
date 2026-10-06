using 'main.bicep'

param location = 'swedencentral'
param publisherEmail = readEnvironmentVariable('HEIMDAL_PUBLISHER_EMAIL', 'heimdal@example.com')
param apimSku = 'StandardV2'

// Models hosted in Microsoft Foundry. Edit freely: name = deployment name used by the router.
// NOTE: model names/versions/formats below must exist in swedencentral for your subscription.
//       `python scripts/deploy.py --check-models` lists what is available and flags mismatches.
param models = [
  {
    name: 'gpt-6-luna'
    model: 'gpt-6-luna'
    version: '1'
    format: 'OpenAI'
    sku: 'GlobalStandard'
    capacity: 100
  }
  {
    name: 'gpt-6-sol'
    model: 'gpt-6-sol'
    version: '1'
    format: 'OpenAI'
    sku: 'GlobalStandard'
    capacity: 50
  }
  {
    name: 'mai-thinking-1'
    model: 'MAI-Thinking-1'
    version: '1'
    format: 'Microsoft'
    sku: 'GlobalStandard'
    capacity: 20
  }
]

// Router tiers -> deployment names above.
param tierModels = {
  pym: 'gpt-6-luna' // Ant-Man: small & quick
  stark: 'gpt-6-sol' // Iron Man: capable all-rounder
  thanos: 'mai-thinking-1' // Thanos: deep reasoning
}
param classifierModel = 'gpt-6-luna'

param router = {
  mode: 'hybrid' // length | complexity | hybrid
  complexityMode: 'heuristic' // heuristic | llm
  lengthThresholdTokens: '500'
  complexityThreshold: '3'
}

param tokenLimits = {
  standard: '5000'
  premium: '100000'
}
