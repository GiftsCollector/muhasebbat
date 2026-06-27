from app import app
import socket

if __name__ == "__main__":
    # Get local hostname
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    
    print(f"\n{'='*60}")
    print(f"🌐 Local URL: http://{local_ip}:5000")
    print(f"🌐 Localhost: http://127.0.0.1:5000")
    print(f"{'='*60}\n")
    print("📝 لمشاركة البرنامج مع الآخرين:")
    print(f"   استخدم: http://{local_ip}:5000")
    print(f"\n{'='*60}\n")
    
    app.run(debug=True, host="0.0.0.0", port=5000)
