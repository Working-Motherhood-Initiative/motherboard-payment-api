from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
import httpx
import os
from dotenv import load_dotenv
import json
from datetime import datetime
import hmac
import hashlib
import sqlalchemy as sa
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

load_dotenv()
app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable not set")

if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

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
    amount = Column(Integer)  # In pesewas
    status = Column(String)
    event_type = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    metadata = Column(String, nullable=True)  # JSON string

# Create tables
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close() 

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY")
PAYSTACK_BASE_URL = "https://api.paystack.co"

headers = {
    "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
    "Content-Type": "application/json"
}

class UserCreate(BaseModel):
    email: str
    first_name: str
    last_name: str
    
    @field_validator('email')
    @classmethod
    def validate_email(cls, v):
        if '@' not in v or '.' not in v:
            raise ValueError('Invalid email format')
        return v.lower()

class SubscriptionCreate(BaseModel):
    email: str
    
    @field_validator('email')
    @classmethod
    def validate_email(cls, v):
        if '@' not in v or '.' not in v:
            raise ValueError('Invalid email format')
        return v.lower()

class PaymentVerification(BaseModel):
    reference: str

class WebhookPayload(BaseModel):
    event: str
    data: dict

@app.get("/")
async def root():
    return {"message": "Motherboard+ Service Payment API", "status": "running"}

@app.get("/health")
async def health_check(db: Session = Depends(get_db)):
    """Health check endpoint for Render"""
    try:
        db.execute(sa.text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database connection failed: {str(e)}")

# 1. CREATE CUSTOMER ENDPOINT
@app.post("/api/customers")
async def create_customer(user: UserCreate, db: Session = Depends(get_db)):
    try:
        # Check if user already exists
        existing_user = db.query(User).filter(User.email == user.email).first()
        if existing_user:
            raise HTTPException(status_code=400, detail="Customer already exists")
        
        async with httpx.AsyncClient() as client:
            payload = {
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name
            }
            
            response = await client.post(
                f"{PAYSTACK_BASE_URL}/customer",
                json=payload,
                headers=headers
            )
            
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to create customer on Paystack")
            
            paystack_customer = response.json()["data"]
            
            # Store customer in database
            db_user = User(
                email=user.email,
                first_name=user.first_name,
                last_name=user.last_name,
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
                    "email": user.email,
                    "customer_code": paystack_customer["customer_code"]
                }
            }
    
    except httpx.HTTPError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error communicating with Paystack: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# 2. INITIALIZE TRANSACTION (First payment to authorize)
@app.post("/api/initialize-payment")
async def initialize_payment(email: str, db: Session = Depends(get_db)):
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="Customer not found. Create customer first.")
        
        async with httpx.AsyncClient() as client:
            # Amount in pesewas (GHS 80 = 8000 pesewas)
            payload = {
                "email": email,
                "amount": 8000,  # GHS 80
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
    
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Error with Paystack: {str(e)}")

# 3. VERIFY PAYMENT
@app.post("/api/verify-payment")
async def verify_payment(verification: PaymentVerification, db: Session = Depends(get_db)):
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{PAYSTACK_BASE_URL}/transaction/verify/{verification.reference}",
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
            
            # Payment successful! Now we have authorization
            customer_email = transaction["customer"]["email"]
            authorization_data = transaction["authorization"]
            amount = transaction["amount"]
            
            # Update user with authorization
            user = db.query(User).filter(User.email == customer_email).first()
            if user:
                user.authorization_code = authorization_data["authorization_code"]
                user.first_authorization = True
                user.updated_at = datetime.utcnow()
                db.commit()
            
            # Log payment
            payment_log = PaymentLog(
                email=customer_email,
                reference=verification.reference,
                amount=amount,
                status="success",
                event_type="initial_payment",
                metadata=json.dumps(authorization_data)
            )
            db.add(payment_log)
            db.commit()
            
            return {
                "status": "success",
                "message": "Payment verified! Customer is now authorized for subscriptions.",
                "data": {
                    "email": customer_email,
                    "amount": amount / 100,  # Convert back to GHS
                    "reference": verification.reference
                }
            }
    
    except httpx.HTTPError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error verifying payment: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# 4. CREATE SUBSCRIPTION (After first payment)
@app.post("/api/create-subscription")
async def create_subscription(subscription: SubscriptionCreate, db: Session = Depends(get_db)):
    try:
        email = subscription.email
        
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="Customer not found")
        
        if not user.authorization_code:
            raise HTTPException(
                status_code=400,
                detail="Customer must complete initial payment first"
            )
        
        async with httpx.AsyncClient() as client:
            # Create subscription using customer's authorization
            payload = {
                "customer": user.paystack_customer_code,
                "plan": "PLN_u6si72zqqto8dq0",  # Your plan ID
                "authorization": user.authorization_code,
                "start_date": datetime.utcnow().isoformat()
            }
            
            response = await client.post(
                f"{PAYSTACK_BASE_URL}/subscription",
                json=payload,
                headers=headers
            )
            
            if response.status_code != 200:
                error_msg = response.json().get("message", "Failed to create subscription")
                raise HTTPException(status_code=400, detail=error_msg)
            
            subscription_data = response.json()["data"]
            
            # Store subscription in database
            db_subscription = Subscription(
                email=email,
                subscription_code=subscription_data["subscription_code"],
                plan_id=subscription_data["plan"],
                status=subscription_data["status"],
                next_payment_date=subscription_data.get("next_payment_date")
            )
            db.add(db_subscription)
            
            # Update user status
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
    
    except httpx.HTTPError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error creating subscription: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# 5. CHECK SUBSCRIPTION STATUS
@app.get("/api/subscription-status/{email}")
async def check_subscription_status(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    
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

# 6. WEBHOOK ENDPOINT (Paystack sends payment notifications here)
@app.post("/api/webhooks/paystack")
async def paystack_webhook(payload: dict, db: Session = Depends(get_db)):
    try:
        # Verify webhook signature
        signature = payload.get("signature")
        
        # Reconstruct the hash
        hash_object = hmac.new(
            PAYSTACK_SECRET_KEY.encode(),
            json.dumps(payload).encode(),
            hashlib.sha512
        )
        
        computed_signature = hash_object.hexdigest()
        
        # Verify signature matches
        if signature != computed_signature:
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        event = payload.get("event")
        data = payload.get("data")
        
        if event == "charge.success":
            # Subscription payment successful
            customer_email = data["customer"]["email"]
            
            user = db.query(User).filter(User.email == customer_email).first()
            if user:
                user.subscription_active = True
                user.last_payment_date = datetime.utcnow()
                user.updated_at = datetime.utcnow()
                db.commit()
            
            # Log payment
            payment_log = PaymentLog(
                email=customer_email,
                reference=data.get("reference"),
                amount=data.get("amount"),
                status="success",
                event_type="charge.success",
                metadata=json.dumps(data)
            )
            db.add(payment_log)
            db.commit()
        
        elif event == "subscription.disable":
            # Subscription disabled/cancelled
            customer_email = data["customer"]["email"]
            
            user = db.query(User).filter(User.email == customer_email).first()
            if user:
                user.subscription_active = False
                user.updated_at = datetime.utcnow()
                db.commit()
        
        return {"status": "ok"}
    
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Webhook processing error: {str(e)}")

# 7. CANCEL SUBSCRIPTION
@app.post("/api/cancel-subscription/{email}")
async def cancel_subscription(email: str, db: Session = Depends(get_db)):
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="Customer not found")
        
        if not user.subscription_code:
            raise HTTPException(status_code=400, detail="No active subscription found")
        
        async with httpx.AsyncClient() as client:
            payload = {"code": user.subscription_code}
            
            response = await client.post(
                f"{PAYSTACK_BASE_URL}/subscription/disable",
                json=payload,
                headers=headers
            )
            
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to cancel subscription")
            
            # Update database
            user.subscription_active = False
            user.updated_at = datetime.utcnow()
            db.commit()
            
            return {
                "status": "success",
                "message": "Subscription cancelled successfully"
            }
    
    except httpx.HTTPError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error cancelling subscription: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# 8. GET ALL CUSTOMERS (For admin dashboard)
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
    uvicorn.run(app, host="0.0.0.0", port=8000)