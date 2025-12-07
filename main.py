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

# Load environment
load_dotenv()
app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable not set")

# SQLAlchemy URL normalization for psycopg
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

# --- Models ---
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    first_name = Column(String)
    last_name = Column(String)
    paystack_customer_code = Column(String)
    paystack_customer_id = Column(Integer)
    authorization_code = Column(String, nullable=True)
    email_token = Column(String, nullable=True)           # <-- store subscription email_token here
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
    email_token = Column(String, nullable=True)          # store token per-subscription as well
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

# --- Helpers ---
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

# --- Routes ---
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

# Create customer
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
                headers=headers,
                timeout=30.0
            )

            if response.status_code not in (200, 201):
                # bubble up useful error from Paystack if present
                try:
                    err = response.json()
                except Exception:
                    err = response.text
                raise HTTPException(status_code=400, detail=f"Failed to create customer on Paystack: {err}")

            paystack_customer = response.json().get("data")

            db_user = User(
                email=email,
                first_name=first_name,
                last_name=last_name,
                paystack_customer_code=paystack_customer.get("customer_code"),
                paystack_customer_id=paystack_customer.get("id"),
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
                    "customer_code": paystack_customer.get("customer_code")
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# Initialize a one-time payment (to collect authorization)
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
                headers=headers,
                timeout=30.0
            )

            if response.status_code not in (200, 201):
                raise HTTPException(status_code=400, detail="Failed to initialize payment")

            transaction_data = response.json().get("data")

            return {
                "status": "success",
                "message": "Payment initialized. Redirect customer to authorization_url",
                "data": {
                    "authorization_url": transaction_data.get("authorization_url"),
                    "access_code": transaction_data.get("access_code"),
                    "reference": transaction_data.get("reference")
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error with Paystack: {str(e)}")

# Verify payment and save authorization
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
                headers=headers,
                timeout=30.0
            )

            if response.status_code not in (200, 201):
                raise HTTPException(status_code=400, detail="Failed to verify payment")

            transaction = response.json().get("data")

            if transaction.get("status") != "success":
                return {
                    "status": "failed",
                    "message": "Payment was not successful"
                }

            customer_email = transaction["customer"]["email"]
            authorization_data = transaction.get("authorization") or {}
            amount = transaction.get("amount")

            user = db.query(User).filter(User.email == customer_email).first()
            if user:
                # Save the card authorization_code (used for charging) but do NOT overwrite email_token
                auth_code = authorization_data.get("authorization_code")
                if auth_code:
                    user.authorization_code = auth_code
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
                    "amount": amount / 100 if amount else None,
                    "reference": reference
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error verifying payment: {str(e)}")

# Create subscription (explicit endpoint)
@app.post("/api/create-subscription")
async def create_subscription(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        email = data.get('email', '').strip()

        if not email:
            raise HTTPException(status_code=400, detail="email is required")

        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=404, detail="Customer not found")

        if not user.authorization_code:
            raise HTTPException(status_code=400, detail="Customer must complete initial payment first")

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
                headers=headers,
                timeout=30.0
            )

            # Paystack returns 200 and JSON with `status` boolean; accept 200/201
            if response.status_code not in (200, 201):
                try:
                    error_msg = response.json().get("message", "Failed to create subscription")
                except Exception:
                    error_msg = response.text or "Failed to create subscription"
                raise HTTPException(status_code=400, detail=error_msg)

            body = response.json()
            # Validate paystack success flag if present
            if not body.get("status"):
                raise HTTPException(status_code=400, detail=body.get("message", "Paystack returned error"))

            subscription_data = body.get("data") or {}

            db_subscription = Subscription(
                email=email,
                subscription_code=subscription_data.get("subscription_code"),
                plan_id=subscription_data.get("plan"),
                status=subscription_data.get("status"),
                next_payment_date=subscription_data.get("next_payment_date"),
                email_token=subscription_data.get("email_token")
            )
            db.add(db_subscription)

            # Save subscription code and email_token on the user
            user.subscription_active = True
            user.subscription_code = subscription_data.get("subscription_code")
            # store email_token separately (do NOT overwrite authorization_code)
            if subscription_data.get("email_token"):
                user.email_token = subscription_data.get("email_token")
            user.updated_at = datetime.utcnow()

            db.commit()

            return {
                "status": "success",
                "message": "Subscription created successfully!",
                "data": {
                    "email": email,
                    "subscription_code": subscription_data.get("subscription_code"),
                    "status": subscription_data.get("status"),
                    "next_payment_date": subscription_data.get("next_payment_date")
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error creating subscription: {str(e)}")

# Check subscription status
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
        "email_token": user.email_token,
        "created_at": user.created_at.isoformat() if user.created_at else None
    }

# Webhook endpoint (signature validated)
@app.post("/api/webhooks/paystack")
async def paystack_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        raw_body = await request.body()
        payload = json.loads(raw_body)

        signature = request.headers.get("X-Paystack-Signature")
        if not signature:
            print("Missing X-Paystack-Signature header")
            return {"status": "error", "message": "Missing signature"}

        computed = hmac.new(
            PAYSTACK_SECRET_KEY.encode(),
            raw_body,
            hashlib.sha512
        ).hexdigest()

        if signature != computed:
            print(f"Invalid webhook signature. Expected: {computed}, Got: {signature}")
            return {"status": "error", "message": "Invalid signature"}

        event = payload.get("event")
        data = payload.get("data") or {}

        # charge.success -> save authorization + optionally auto-create subscription
        if event == "charge.success":
            customer_email = None
            # customer may be nested depending on payload
            if isinstance(data.get("customer"), dict):
                customer_email = data["customer"].get("email")

            if not customer_email:
                return {"status": "ok", "message": "No customer email found"}

            user = db.query(User).filter(User.email == customer_email).first()
            if user:
                # save card authorization if present
                authorization = data.get("authorization") or {}
                if authorization.get("authorization_code"):
                    user.authorization_code = authorization.get("authorization_code")
                    user.first_authorization = True
                user.subscription_active = True
                user.last_payment_date = datetime.utcnow()
                user.updated_at = datetime.utcnow()
                db.commit()

                # Auto-create subscription only if we have authorization and no subscription yet
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
                                headers=headers,
                                timeout=30.0
                            )

                            if response.status_code in (200, 201):
                                body = response.json()
                                if body.get("status"):
                                    subscription_data = body.get("data") or {}
                                    db_subscription = Subscription(
                                        email=customer_email,
                                        subscription_code=subscription_data.get("subscription_code"),
                                        plan_id=subscription_data.get("plan"),
                                        status=subscription_data.get("status"),
                                        next_payment_date=subscription_data.get("next_payment_date"),
                                        email_token=subscription_data.get("email_token")
                                    )
                                    db.add(db_subscription)
                                    user.subscription_code = subscription_data.get("subscription_code")
                                    if subscription_data.get("email_token"):
                                        user.email_token = subscription_data.get("email_token")
                                    db.commit()
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

        # subscription.disable -> mark subscription inactive in our DB
        elif event == "subscription.disable" or event == "subscription.not_renew":
            customer_email = None
            if isinstance(data.get("customer"), dict):
                customer_email = data["customer"].get("email")

            if not customer_email:
                return {"status": "ok", "message": "No customer email found"}

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

# Cancel subscription (fixed)
@app.post("/api/cancel-subscription/{email}")
async def cancel_subscription(email: str, db: Session = Depends(get_db)):
    try:
        if not email:
            raise HTTPException(status_code=400, detail="email is required")

        user = db.query(User).filter(User.email == email.lower()).first()
        if not user:
            raise HTTPException(status_code=404, detail="Customer not found")

        if not user.subscription_code:
            raise HTTPException(status_code=400, detail="No active subscription found")

        # token can be on user.email_token or in the subscriptions table
        token = user.email_token
        if not token:
            sub = db.query(Subscription).filter(Subscription.subscription_code == user.subscription_code).first()
            if sub:
                token = sub.email_token

        if not token:
            raise HTTPException(status_code=400, detail="Missing email_token for subscription cancellation")

        async with httpx.AsyncClient() as client:
            payload = {
                "code": user.subscription_code,
                "token": token
            }

            response = await client.post(
                f"{PAYSTACK_BASE_URL}/subscription/disable",
                json=payload,
                headers=headers,
                timeout=30.0
            )

            # Accept 200/201 and Paystack 'status' true
            if response.status_code not in (200, 201):
                try:
                    err = response.json()
                except Exception:
                    err = response.text
                raise HTTPException(status_code=400, detail=f"Failed to cancel subscription: {err}")

            body = response.json()
            if not body.get("status"):
                raise HTTPException(status_code=400, detail=body.get("message", "Paystack returned error"))

        # Our local update
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
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error cancelling subscription: {str(e)}")

# Admin listing
@app.get("/api/admin/customers")
async def get_all_customers(db: Session = Depends(get_db)):
    users = db.query(User).all()

    customers = [
        {
            "email": user.email,
            "name": f"{user.first_name} {user.last_name}",
            "subscription_active": user.subscription_active,
            "subscription_code": user.subscription_code,
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
