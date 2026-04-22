@description('Name of the azd environment used for stable per-environment resource names.')
param environmentName string

@description('The location for all resources.')
param location string

@description('Tags to apply to all resources.')
param tags object

@description('Unique resource token derived from the subscription, environment name, and location.')
@minLength(2)
param resourceToken string

@description('Last.fm API key stored as a Container App secret.')
@secure()
param lastfmApiKey string = ''

var normalizedEnvironmentName = toLower(replace(replace(environmentName, '_', '-'), ' ', '-'))
var compactEnvironmentName = take(toLower(replace(replace(replace(replace(environmentName, '-', ''), '_', ''), ' ', ''), '.', '')), 41)
var acrName = 'acr${compactEnvironmentName}${take(resourceToken, 6)}'
var logAnalyticsName = take('log-${normalizedEnvironmentName}', 63)
var containerAppsEnvironmentName = take('cae-${normalizedEnvironmentName}', 32)
var containerAppName = take('ca-${normalizedEnvironmentName}', 32)
var cosmosAccountName = take('cosmos-${normalizedEnvironmentName}-${take(resourceToken, 6)}', 50)
var cosmosDatabaseName = 'lastfm-timetraveler'
var cosmosContainerName = 'searches'

// ---------------------------------------------------------------------------
// Log Analytics Workspace (required by Container Apps Environment)
// ---------------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
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
  name: acrName
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
// Azure Cosmos DB for NoSQL (serverless cache store)
// ---------------------------------------------------------------------------
resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: cosmosAccountName
  location: location
  kind: 'GlobalDocumentDB'
  tags: tags
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    backupPolicy: {
      type: 'Periodic'
      periodicModeProperties: {
        backupIntervalInMinutes: 240
        backupRetentionIntervalInHours: 8
        backupStorageRedundancy: 'Local'
      }
    }
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
    minimalTlsVersion: 'Tls12'
  }
}

resource cosmosDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  name: cosmosDatabaseName
  parent: cosmosAccount
  properties: {
    resource: {
      id: cosmosDatabaseName
    }
  }
}

resource cosmosContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  name: cosmosContainerName
  parent: cosmosDatabase
  properties: {
    resource: {
      id: cosmosContainerName
      partitionKey: {
        paths: [
          '/username_normalized'
        ]
        kind: 'Hash'
        version: 2
      }
    }
  }
}

resource cosmosSpotifyProfilesContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  name: 'spotify_profiles'
  parent: cosmosDatabase
  properties: {
    resource: {
      id: 'spotify_profiles'
      partitionKey: {
        paths: [
          '/profile_id_normalized'
        ]
        kind: 'Hash'
        version: 2
      }
    }
  }
}

resource cosmosSpotifyPlaysContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  name: 'spotify_plays'
  parent: cosmosDatabase
  properties: {
    resource: {
      id: 'spotify_plays'
      partitionKey: {
        paths: [
          '/profile_id_normalized'
        ]
        kind: 'Hash'
        version: 2
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Container Apps Environment
// ---------------------------------------------------------------------------
resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: containerAppsEnvironmentName
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
  name: containerAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  dependsOn: [
    cosmosContainer
    cosmosSpotifyProfilesContainer
    cosmosSpotifyPlaysContainer
  ]
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
        {
          name: 'cosmos-connection-string'
          value: cosmosAccount.listConnectionStrings().connectionStrings[0].connectionString
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
              name: 'COSMOS_CONNECTION_STRING'
              secretRef: 'cosmos-connection-string'
            }
            {
              name: 'COSMOS_DATABASE_NAME'
              value: cosmosDatabaseName
            }
            {
              name: 'COSMOS_CONTAINER_NAME'
              value: cosmosContainerName
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/api/ready'
                port: 5000
              }
              initialDelaySeconds: 10
              periodSeconds: 5
              timeoutSeconds: 10
              failureThreshold: 24
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/api/ready'
                port: 5000
              }
              initialDelaySeconds: 10
              periodSeconds: 10
              timeoutSeconds: 10
              failureThreshold: 3
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/api/status'
                port: 5000
              }
              initialDelaySeconds: 30
              periodSeconds: 30
              timeoutSeconds: 3
              failureThreshold: 3
            }
          ]
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
output AZURE_COSMOS_DB_ACCOUNT_NAME string = cosmosAccount.name
output AZURE_COSMOS_DB_ENDPOINT string = cosmosAccount.properties.documentEndpoint
