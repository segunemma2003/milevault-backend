from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.database import get_db
from app.schemas.message import ChatMessageCreate, DirectMessageCreate
from app.models.message import ChatMessage, DirectMessage
from app.models.transaction import Transaction
from app.models.user import User
from app.dependencies import get_current_user

router = APIRouter(tags=["messages"])


def msg_to_dict(m: ChatMessage) -> dict:
    return {
        "id": m.id,
        "transaction_id": m.transaction_id,
        "sender_id": m.sender_id,
        "sender": {
            "id": m.sender.id,
            "first_name": m.sender.first_name,
            "last_name": m.sender.last_name,
            "name": m.sender.first_name + ' ' + sender.last_name,
            "email": m.sender.email,
            "role": m.sender.role,
            "avatar_url": m.sender.avatar_url,
            "is_kyc_verified": m.sender.is_kyc_verified,
            "created_at": m.sender.created_at,
        } if m.sender else None,
        "message": m.message,
        "attachments": m.attachments or [],
        "created_at": m.created_at,
    }


def direct_msg_to_dict(m: DirectMessage) -> dict:
    return {
        "id": m.id,
        "sender_id": m.sender_id,
        "recipient_id": m.recipient_id,
        "sender": {
            "id": m.sender.id,
            "name": m.sender.first_name + ' ' + sender.last_name,
            "email": m.sender.email,
            "avatar_url": m.sender.avatar_url,
            "created_at": m.sender.created_at,
        } if m.sender else None,
        "recipient": {
            "id": m.recipient.id,
            "name": m.recipient.first_name + ' ' + recipient.last_name,
            "email": m.recipient.email,
            "avatar_url": m.recipient.avatar_url,
            "created_at": m.recipient.created_at,
        } if m.recipient else None,
        "message": m.message,
        "is_read": m.is_read,
        "created_at": m.created_at,
    }


@router.get("/transactions/{transaction_id}/messages")
def get_chat_messages(
    transaction_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id and tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    messages = db.query(ChatMessage).filter(
        ChatMessage.transaction_id == transaction_id
    ).order_by(ChatMessage.created_at.asc()).all()
    return [msg_to_dict(m) for m in messages]


@router.post("/transactions/{transaction_id}/messages", status_code=201)
def send_chat_message(
    transaction_id: str,
    payload: ChatMessageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id and tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    msg = ChatMessage(
        transaction_id=transaction_id,
        sender_id=current_user.id,
        message=payload.message,
        attachments=payload.attachments or [],
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg_to_dict(msg)


@router.get("/messages")
def get_direct_messages(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    messages = db.query(DirectMessage).filter(
        (DirectMessage.sender_id == current_user.id) | (DirectMessage.recipient_id == current_user.id)
    ).order_by(DirectMessage.created_at.desc()).all()
    return [direct_msg_to_dict(m) for m in messages]


@router.post("/messages", status_code=201)
def send_direct_message(
    payload: DirectMessageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    recipient = db.query(User).filter(User.id == payload.recipient_id).first()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")

    msg = DirectMessage(
        sender_id=current_user.id,
        recipient_id=payload.recipient_id,
        message=payload.message,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return direct_msg_to_dict(msg)


@router.put("/messages/{message_id}/read")
def mark_message_read(
    message_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    msg = db.query(DirectMessage).filter(
        DirectMessage.id == message_id, DirectMessage.recipient_id == current_user.id
    ).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    msg.is_read = True
    db.commit()
    return {"message": "Marked as read"}
