@description('The location for all resources.')
param location string

@description('Tags to apply to all resources.')
param tags object

@description('Unique resource token derived from the subscription, environment name, and location.')
param resourceToken string

@description('Last.fm API key stored as a Container App secret.')
@secure()
param lastfmApiKey string = ''

@description('Last.fm username exposed as an environment variable.')
param lastfmUsername string = ''

// ---------------------------------------------------------------------------
// Log Analytics Workspace (required by Container Apps Environment)
// ---------------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: 'log-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// ---------------------------------------------------------------------------
// Azure Container Registry
// ---------------------------------------------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-01-01-preview' = {
  name: 'acr${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: true
  }
}

// ---------------------------------------------------------------------------
// Container Apps Environment
// ---------------------------------------------------------------------------
resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: 'cae-${resourceToken}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Container App
// ---------------------------------------------------------------------------
resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: 'ca-${resourceToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 5000
        transport: 'auto'
      }
      registries: [
        {
          server: acr.properties.loginServer
          username: acr.listCredentials().username
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: [
        {
          name: 'acr-password'
          value: acr.listCredentials().passwords[0].value
        }
        {
          name: 'lastfm-api-key'
          value: lastfmApiKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          // azd replaces this placeholder with the image it builds and pushes to ACR during `azd deploy`.
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          env: [
            {
              name: 'LASTFM_API_KEY'
              secretRef: 'lastfm-api-key'
            }
            {
              name: 'LASTFM_USERNAME'
              value: lastfmUsername
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        // Set to 0 to minimise cost; increase to 1 if cold-start latency is unacceptable.
        minReplicas: 0
        maxReplicas: 10
      }
    }
  }
}

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.properties.loginServer
output AZURE_CONTAINER_REGISTRY_NAME string = acr.name
output SERVICE_WEB_URI string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
