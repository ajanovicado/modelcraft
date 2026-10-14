fn main() {
    println!("cargo:rerun-if-env-changed=CARGO_FEATURE_SOURCE_CHECK");
    if std::env::var_os("CARGO_FEATURE_SOURCE_CHECK").is_some() {
        assert!(
            std::env::var("PROFILE").as_deref() == Ok("debug")
                && std::env::var("DEBUG").as_deref() == Ok("true"),
            "source-check is restricted to debug source compilation; release builds require the verified engine and runtime payload"
        );
        // Tauri also copies bundle resources during cargo check. Only this
        // explicit compilation feature omits payloads; normal builds keep the
        // production config and its missing-resource failures.
        let mut config = std::env::var("TAURI_CONFIG")
            .map(|value| {
                serde_json::from_str::<serde_json::Value>(&value)
                    .expect("TAURI_CONFIG must be valid JSON")
            })
            .unwrap_or_else(|_| serde_json::json!({}));
        let config_object = config
            .as_object_mut()
            .expect("TAURI_CONFIG must be an object");
        let bundle = config_object
            .entry("bundle")
            .or_insert_with(|| serde_json::json!({}));
        let bundle = bundle
            .as_object_mut()
            .expect("TAURI_CONFIG bundle must be an object");
        bundle.insert("active".into(), serde_json::json!(false));
        bundle.insert("resources".into(), serde_json::json!([]));
        bundle.insert("externalBin".into(), serde_json::json!([]));
        let config = serde_json::to_string(&config).expect("source-check config must serialize");
        std::env::set_var("TAURI_CONFIG", &config);
        println!("cargo:rustc-env=TAURI_CONFIG={config}");
    }
    tauri_build::build()
}
