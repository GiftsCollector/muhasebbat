from app import app
import os

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_ENV") != "production"
    app.run(host="0.0.0.0", port=port, debug=debug)
    
    if debug_mode:
        print(f"\n{'='*60}")
        print(f"🌐 Local URL: http://{local_ip}:5000")
        print(f"🌐 Localhost: http://127.0.0.1:5000")
        print(f"{'='*60}\n")
    
    app.run(debug=debug_mode, host="0.0.0.0", port=int(os.getenv('PORT', 5000)))
