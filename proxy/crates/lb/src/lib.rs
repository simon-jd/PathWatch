pub mod least_request;
pub mod maglev;
pub type EndpointId = u64;
pub trait Picker: Send + Sync {
    fn pick(&self, key: Option<u64>) -> Option<EndpointId>;
}
