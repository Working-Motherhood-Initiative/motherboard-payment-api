from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
from dotenv import load_dotenv
import json
from datetime import datetime
import hmac
import hashlib
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

load_dotenv()
app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable not set")

if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
elif not DATABASE_URL.startswith("postgresql+psycopg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    first_name = Column(String)
    last_name = Column(String)
    paystack_customer_code = Column(String)
    paystack_customer_id = Column(Integer)
    authorization_code = Column(String, nullable=True)
    first_authorization = Column(Boolean, default=False)
    subscription_active = Column(Boolean, default=False)
    subscription_code = Column(String, nullable=True)
    last_payment_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Subscription(Base):
    __tablename__ = "subscriptions"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, index=True)
    subscription_code = Column(String, unique=True, index=True)
    plan_id = Column(String)
    status = Column(String)
    next_payment_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class PaymentLog(Base):
    __tablename__ = "payment_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, index=True)
    reference = Column(String, unique=True, index=True)
    amount = Column(Integer)
    status = Column(String)
    event_type = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    metadata_json = Column(String, nullable=True)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://dragonfly-chihuahua-alhg.squarespace.com"],
    allow_credentials=True,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY")
PAYSTACK_BASE_URL = "https://api.paystack.co"

headers = {
    "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
    "Content-Type": "application/json"
}

def validate_email(email: str) -> str:
    if not email or '@' not in email or '.' not in email:
        raise ValueError('Invalid email format')
    return email.lower().strip()

@app.get("/")
async def root():
    return {"message": "Motherboard+ Service Payment API", "status": "running"}

@app.get("/health")
async def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database connection failed: {str(e)}")

@app.post("/api/customers")
async def create_customer(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        email = data.get('email', '').strip()
        first_name = data.get('first_name', '').strip()
        last_name = data.get('last_name', '').strip()
        
        if not email or not first_name or not last_name:
            raise HTTPException(status_code=400, detail="email, first_name, and last_name are required")
        
        validate_email(email)
        
        existing_user = db.query(User).filter(User.email == email).first()
        if existing_user:
            raise HTTPException(status_code=400, detail="Customer already exists")
        
        async with httpx.AsyncClient() as client:
            payload = {
                "email": email,
                "first_name": first_name,
                "last_name": last_name
            }
            
            response = await client.post(
                f"{PAYSTACK_BASE_URL}/customer",
                json=payload,
                headers=headers
            )
            
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to create customer on Paystack")
            
            paystack_customer = response.json()["data"]
            
            db_user = User(
                email=email,
                first_name=first_name,
                last_name=last_name,
                paystack_customer_code=paystack_customer["customer_code"],
                paystack_customer_id=paystack_customer["id"],
                subscription_active=False
            )
            db.add(db_user)
            db.commit()
            db.refresh(db_user)
            
            return {
                "status": "success",
                "message": "Customer created successfully",
                "data": {
                    "email": email,
                    "customer_code": paystack_customer["customer_code"]
                }
            }
    
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/initialize-payment")
async def initialize_payment(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        email = data.get('email', '').strip()
        
        if not email:
            raise HTTPException(status_code=400, detail="email is required")
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="Customer not found. Create customer first.")
        
        async with httpx.AsyncClient() as client:
            payload = {
                "email": email,
                "amount": 8000,
                "channels": ["card", "bank", "ussd", "qr", "mobile_money"],
                "metadata": {
                    "plan_name": "Motherboard Monthly Plan",
                    "subscription_type": "monthly"
                }
            }
            
            response = await client.post(
                f"{PAYSTACK_BASE_URL}/transaction/initialize",
                json=payload,
                headers=headers
            )
            
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to initialize payment")
            
            transaction_data = response.json()["data"]
            
            return {
                "status": "success",
                "message": "Payment initialized. Redirect customer to authorization_url",
                "data": {
                    "authorization_url": transaction_data["authorization_url"],
                    "access_code": transaction_data["access_code"],
                    "reference": transaction_data["reference"]
                }
            }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error with Paystack: {str(e)}")

@app.post("/api/verify-payment")
async def verify_payment(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        reference = data.get('reference', '').strip()
        
        if not reference:
            raise HTTPException(status_code=400, detail="reference is required")
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{PAYSTACK_BASE_URL}/transaction/verify/{reference}",
                headers=headers
            )
            
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to verify payment")
            
            transaction = response.json()["data"]
            
            if transaction["status"] != "success":
                return {
                    "status": "failed",
                    "message": "Payment was not successful"
                }
            
            customer_email = transaction["customer"]["email"]
            authorization_data = transaction["authorization"]
            amount = transaction["amount"]
            
            user = db.query(User).filter(User.email == customer_email).first()
            if user:
                user.authorization_code = authorization_data["authorization_code"]
                user.first_authorization = True
                user.updated_at = datetime.utcnow()
                db.commit()
            
            payment_log = PaymentLog(
                email=customer_email,
                reference=reference,
                amount=amount,
                status="success",
                event_type="initial_payment",
                metadata_json=json.dumps(authorization_data)
            )
            db.add(payment_log)
            db.commit()
            
            return {
                "status": "success",
                "message": "Payment verified! Customer is now authorized for subscriptions.",
                "data": {
                    "email": customer_email,
                    "amount": amount / 100,
                    "reference": reference
                }
            }
    
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error verifying payment: {str(e)}")

@app.post("/api/create-subscription")
async def create_subscription(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        email = data.get('email', '').strip()
        
        print(f"Create subscription request for: {email}")
        
        if not email:
            raise HTTPException(status_code=400, detail="email is required")
        
        user = db.query(User).filter(User.email == email).first()
        print(f"User found: {user}")
        
        if not user:
            raise HTTPException(status_code=404, detail="Customer not found")
        
        print(f"Authorization code: {user.authorization_code}")
        
        if not user.authorization_code:
            raise HTTPException(
                status_code=400,
                detail="Customer must complete initial payment first"
            )
        
        async with httpx.AsyncClient() as client:
            payload = {
                "customer": user.paystack_customer_code,
                "plan": "PLN_u6si72zqqto8dq0",
                "authorization": user.authorization_code,
                "start_date": datetime.utcnow().isoformat()
            }
            
            response = await client.post(
                f"{PAYSTACK_BASE_URL}/subscription",
                json=payload,
                headers=headers
            )
            
            print(f"Paystack subscription response: {response.status_code}")
            print(f"Paystack response body: {response.text}")
            
            if response.status_code != 200:
                error_msg = response.json().get("message", "Failed to create subscription")
                print(f"Subscription creation failed: {error_msg}")
                raise HTTPException(status_code=400, detail=error_msg)
            
            subscription_data = response.json()["data"]
            
            db_subscription = Subscription(
                email=email,
                subscription_code=subscription_data["subscription_code"],
                plan_id=subscription_data["plan"],
                status=subscription_data["status"],
                next_payment_date=subscription_data.get("next_payment_date")
            )
            db.add(db_subscription)
            
            user.subscription_active = True
            user.subscription_code = subscription_data["subscription_code"]
            user.updated_at = datetime.utcnow()
            
            db.commit()
            
            return {
                "status": "success",
                "message": "Subscription created successfully!",
                "data": {
                    "email": email,
                    "subscription_code": subscription_data["subscription_code"],
                    "status": subscription_data["status"],
                    "next_payment_date": subscription_data.get("next_payment_date")
                }
            }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Create subscription error: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error creating subscription: {str(e)}")

@app.get("/api/subscription-status/{email}")
async def check_subscription_status(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email.lower()).first()
    
    if not user:
        return {
            "status": "not_found",
            "subscription_active": False
        }
    
    return {
        "status": "found",
        "subscription_active": user.subscription_active,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "subscription_code": user.subscription_code,
        "created_at": user.created_at.isoformat() if user.created_at else None
    }

@app.post("/api/webhooks/paystack")
async def paystack_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        # Get raw body for signature validation
        raw_body = await request.body()
        payload = json.loads(raw_body)
        
        signature = request.headers.get("X-Paystack-Signature")
        hash_object = hmac.new(
            PAYSTACK_SECRET_KEY.encode(),
            raw_body,
            hashlib.sha512
        )
        
        computed_signature = hash_object.hexdigest()
        
        if signature != computed_signature:
            print(f"Invalid webhook signature. Expected: {computed_signature}, Got: {signature}")
            return {"status": "error", "message": "Invalid signature"}
        
        print("Signature validated")
        event = payload.get("event")
        data = payload.get("data")
        
        if event == "charge.success":
            if not data or "customer" not in data or "email" not in data.get("customer", {}):
                return {"status": "ok", "message": "No customer email found"}
            
            customer_email = data["customer"]["email"]
            
            user = db.query(User).filter(User.email == customer_email).first()
            if user:
                if "authorization" in data and "authorization_code" in data["authorization"]:
                    user.authorization_code = data["authorization"]["authorization_code"]
                    user.first_authorization = True
                user.subscription_active = True
                user.last_payment_date = datetime.utcnow()
                user.updated_at = datetime.utcnow()
                db.commit()
                
                # Auto-create subscription immediately
                if user.authorization_code and not user.subscription_code:
                    try:
                        async with httpx.AsyncClient() as client:
                            payload = {
                                "customer": user.paystack_customer_code,
                                "plan": "PLN_u6si72zqqto8dq0",
                                "authorization": user.authorization_code,
                                "start_date": datetime.utcnow().isoformat()
                            }
                            
                            response = await client.post(
                                f"{PAYSTACK_BASE_URL}/subscription",
                                json=payload,
                                headers=headers
                            )
                            
                            if response.status_code == 200:
                                subscription_data = response.json()["data"]
                                
                                db_subscription = Subscription(
                                    email=customer_email,
                                    subscription_code=subscription_data["subscription_code"],
                                    plan_id=subscription_data["plan"],
                                    status=subscription_data["status"],
                                    next_payment_date=subscription_data.get("next_payment_date")
                                )
                                db.add(db_subscription)
                                user.subscription_code = subscription_data["subscription_code"]
                                db.commit()
                                print(f"Subscription created: {subscription_data['subscription_code']}")
                    except Exception as e:
                        print(f"Error creating subscription in webhook: {str(e)}")
            
            payment_log = PaymentLog(
                email=customer_email,
                reference=data.get("reference"),
                amount=data.get("amount"),
                status="success",
                event_type="charge.success",
                metadata_json=json.dumps(data)
            )
            db.add(payment_log)
            db.commit()
        
        elif event == "subscription.disable":
            if not data or "customer" not in data or "email" not in data.get("customer", {}):
                return {"status": "ok", "message": "No customer email found"}
            
            customer_email = data["customer"]["email"]
            
            user = db.query(User).filter(User.email == customer_email).first()
            if user:
                user.subscription_active = False
                user.updated_at = datetime.utcnow()
                db.commit()
        
        return {"status": "ok"}
    
    except Exception as e:
        print(f"Webhook error: {str(e)}")
        db.rollback()
        return {"status": "error", "message": str(e)}

@app.post("/api/cancel-subscription/{email}")
async def cancel_subscription(email: str, db: Session = Depends(get_db)):
    try:
        print(f"Cancel subscription request for: {email}")
        
        if not email:
            raise HTTPException(status_code=400, detail="email is required")
        
        user = db.query(User).filter(User.email == email.lower()).first()
        print(f"User found: {user}")
        
        if not user:
            raise HTTPException(status_code=404, detail="Customer not found")
        
        print(f"Subscription code: {user.subscription_code}")
        
        if not user.subscription_code:
            raise HTTPException(status_code=400, detail="No active subscription found")
        
        async with httpx.AsyncClient() as client:
            payload = {
                "token": user.authorization_code
            }
            
            response = await client.post(
                f"{PAYSTACK_BASE_URL}/subscription/{user.subscription_code}/disable",
                json=payload,
                headers=headers
            )
            
            print(f"Paystack response: {response.status_code}")
            print(f"Paystack response body: {response.text}")
            
            if response.status_code != 200:
                error_msg = response.json().get("message", "Failed to cancel subscription")
                print(f"Cancel failed: {error_msg}")
                raise HTTPException(status_code=400, detail=error_msg)
            
            user.subscription_active = False
            user.updated_at = datetime.utcnow()
            db.commit()
            
            return {
                "status": "success",
                "message": "Subscription cancelled successfully"
            }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Cancel subscription error: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error cancelling subscription: {str(e)}")

@app.get("/api/admin/customers")
async def get_all_customers(db: Session = Depends(get_db)):
    users = db.query(User).all()
    
    customers = [
        {
            "email": user.email,
            "name": f"{user.first_name} {user.last_name}",
            "subscription_active": user.subscription_active,
            "created_at": user.created_at.isoformat() if user.created_at else None
        }
        for user in users
    ]
    
    return {
        "total_customers": len(customers),
        "customers": customers
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)