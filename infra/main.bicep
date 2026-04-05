targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment that is used to generate a short unique hash for resources.')
param environmentName string

@minLength(1)
@description('Primary location for all resources.')
param location string

@description('Last.fm API key.')
@secure()
param lastfmApiKey string = ''

@description('Last.fm username.')
param lastfmUsername string = ''

var tags = { 'azd-env-name': environmentName }
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))

resource rg 'Microsoft.Resources/resourceGroups@2022-09-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    lastfmApiKey: lastfmApiKey
    lastfmUsername: lastfmUsername
  }
}

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.AZURE_CONTAINER_REGISTRY_ENDPOINT
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.AZURE_CONTAINER_REGISTRY_NAME
output SERVICE_WEB_URI string = resources.outputs.SERVICE_WEB_URI
