from app import app
import socket
import os

if __name__ == "__main__":
    # Get local hostname
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    
    debug_mode = os.getenv('FLASK_ENV') != 'production'
    
    if debug_mode:
        print(f"\n{'='*60}")
        print(f"🌐 Local URL: http://{local_ip}:5000")
        print(f"🌐 Localhost: http://127.0.0.1:5000")
        print(f"{'='*60}\n")
    
    app.run(debug=debug_mode, host="0.0.0.0", port=int(os.getenv('PORT', 5000)))
