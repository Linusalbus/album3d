plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// CI sets GITHUB_RUN_NUMBER so every build installs as an update over the last one.
val build = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()

android {
    namespace = "dk.linusalbus.claudepocket"
    compileSdk = 34

    defaultConfig {
        applicationId = "dk.linusalbus.claudepocket"
        minSdk = 28
        targetSdk = 34
        versionCode = build
        versionName = "1.0.$build"
    }

    // A fixed key so new builds install over old ones. Personal app, private repo.
    signingConfigs {
        create("release") {
            storeFile = file("pocket-release.jks")
            storePassword = "claudepocket"
            keyAlias = "pocket"
            keyPassword = "claudepocket"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}
