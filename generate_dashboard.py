import database as db
import dashboard

if __name__ == "__main__":
    db.init_db()
    html_content = dashboard._render_dashboard()
    
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
        
    print(" Dashboard index.html succesvol gegenereerd!")
    